import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.parse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from config import BOT_CATALOG, BUNDLE_CATALOG, get_env_config
from db import Database
from mailer import Mailer


def sign_session_token(secret: str, issued_at: int) -> str:
    payload = f"aurum_admin:{issued_at}"
    sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    raw = f"{payload}:{sig}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def verify_session_token(token: str, secret: str, max_age_sec: int = 86400 * 7) -> bool:
    if not token:
        return False
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        parts = raw.split(":")
        if len(parts) != 3 or parts[0] != "aurum_admin":
            return False
        issued_at = int(parts[1])
        if time.time() - issued_at > max_age_sec:
            return False
        expected = hmac.new(
            secret.encode("utf-8"),
            f"{parts[0]}:{parts[1]}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(parts[2], expected)
    except Exception:
        return False


class AurumHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, RequestHandlerClass, db: Database, mailer: Mailer, env_cfg: Dict[str, str], store_html: bytes, admin_html: bytes):
        self.db = db
        self.mailer = mailer
        self.env_cfg = env_cfg
        self.store_html = store_html
        self.admin_html = admin_html
        super().__init__(server_address, RequestHandlerClass)


class AurumRequestHandler(BaseHTTPRequestHandler):
    server_version = "AurumDeskServer/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    @property
    def db(self) -> Database:
        return self.server.db  # type: ignore[attr-defined]

    @property
    def mailer(self) -> Mailer:
        return self.server.mailer  # type: ignore[attr-defined]

    @property
    def env_cfg(self) -> Dict[str, str]:
        return self.server.env_cfg  # type: ignore[attr-defined]

    def _client_ip(self) -> str:
        fwd = self.headers.get("X-Forwarded-For", "").strip()
        if fwd:
            return fwd.split(",")[0].strip()
        return self.client_address[0] if self.client_address else ""

    def _is_https(self) -> bool:
        return self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def _is_admin(self) -> bool:
        cookie_hdr = self.headers.get("Cookie", "")
        if not cookie_hdr:
            return False
        c = SimpleCookie()
        try:
            c.load(cookie_hdr)
        except Exception:
            return False
        morsel = c.get("aurum_session")
        if not morsel:
            return False
        return verify_session_token(morsel.value, self.env_cfg["SECRET_KEY"])

    def _send_html(self, data: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: Any, status: int = 200, extra_headers: Optional[Dict[str, str]] = None) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self, max_bytes: int = 3_500_000) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        if length > max_bytes:
            raise ValueError("Payload too large (max 3.5 MB).")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._send_html(self.server.store_html)  # type: ignore[attr-defined]
            return

        if path == "/admin" or path == "/admin/":
            self._send_html(self.server.admin_html)  # type: ignore[attr-defined]
            return

        if path == "/health":
            self._send_json({"ok": True, "service": "AurumDeskServer", "postgres": self.db.is_postgres})
            return

        if path == "/api/catalog":
            settings = self.db.get_settings()
            self._send_json({
                "ok": True,
                "bots": BOT_CATALOG,
                "bundles": BUNDLE_CATALOG,
                "paypal_me_url": self.env_cfg["PAYPAL_ME_URL"],
                "contact_email": settings.get("admin_notify_email") or self.env_cfg["PUBLIC_CONTACT_EMAIL"],
                "store_notice": settings.get("store_notice", ""),
            })
            return

        if path.startswith("/api/order-status/"):
            ref = urllib.parse.unquote(path.split("/api/order-status/", 1)[1]).strip()
            order = self.db.get_order_by_ref(ref, include_screenshot=False)
            if not order:
                self._send_json({"ok": False, "error": "Order reference not found."}, status=404)
                return
            # Do not expose full license_key publicly on unauthenticated status tracker
            safe_order = {
                "order_ref": order["order_ref"],
                "customer_email": order["customer_email"],
                "mt5_login": order["mt5_login"],
                "amount_eur": order["amount_eur"],
                "status": order["status"],
                "admin_note": order["admin_note"] if order["status"] == "rejected" else "",
            }
            self._send_json({"ok": True, "order": safe_order})
            return

        if path == "/admin/api/overview":
            if not self._is_admin():
                self._send_json({"ok": False, "error": "Unauthorized"}, status=401)
                return
            notif_data = self.db.get_notifications(limit=80)
            self._send_json({
                "ok": True,
                "bots": BOT_CATALOG,
                "orders": self.db.get_orders(status_filter="all", include_screenshots=False),
                "licenses": self.db.get_licenses(),
                "notifications": notif_data["items"],
                "unread_count": notif_data["unread_count"],
                "email_logs": self.db.get_email_logs(limit=60),
                "settings": self.db.get_settings(),
                "metrics": self.db.get_dashboard_metrics(),
                "is_postgres": self.db.is_postgres,
                "mailer_configured": self.mailer.is_configured(),
            })
            return

        if path.startswith("/admin/api/orders/"):
            if not self._is_admin():
                self._send_json({"ok": False, "error": "Unauthorized"}, status=401)
                return
            ref = urllib.parse.unquote(path.split("/admin/api/orders/", 1)[1]).strip("/")
            order = self.db.get_order_by_ref(ref, include_screenshot=True)
            if not order:
                self._send_json({"ok": False, "error": "Order not found"}, status=404)
                return
            self._send_json({"ok": True, "order": order})
            return

        self._send_json({"ok": False, "error": "Not found"}, status=404)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        try:
            body = self._read_json()
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
            return

        # 1) Public customer order submission
        if path == "/api/orders":
            try:
                order = self.db.create_order(
                    customer_name=str(body.get("customer_name", "")),
                    customer_email=str(body.get("customer_email", "")),
                    mt5_login=str(body.get("mt5_login", "")),
                    plan_type=str(body.get("plan_type", "lifetime")),
                    bot_ids=list(body.get("bot_ids") or []),
                    bundle_id=str(body.get("bundle_id", "")),
                    paypal_tx=str(body.get("paypal_tx", "")),
                    screenshot_data=str(body.get("screenshot_data", "")),
                )
                self._send_json({"ok": True, "order": self.db._format_order(order, include_screenshot=False)}, status=201)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            return

        # 2) Public desktop app license verification endpoint
        if path == "/api/license/verify":
            res = self.db.verify_license(
                license_key=str(body.get("license_key", "")),
                mt5_login=str(body.get("mt5_login", "")),
                client_ip=self._client_ip(),
            )
            status = 200 if res.get("ok") else (403 if res.get("error") == "account_mismatch" else 400)
            self._send_json(res, status=status)
            return

        # 3) Admin authentication
        if path == "/admin/api/login":
            pwd = str(body.get("password", ""))
            expected = self.env_cfg["ADMIN_PASSWORD"]
            if hmac.compare_digest(pwd, expected):
                token = sign_session_token(self.env_cfg["SECRET_KEY"], int(time.time()))
                sec_flag = "; Secure" if self._is_https() else ""
                cookie = f"aurum_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={86400 * 7}{sec_flag}"
                self._send_json({"ok": True}, extra_headers={"Set-Cookie": cookie})
            else:
                time.sleep(0.35)
                self._send_json({"ok": False, "error": "Invalid Admin Password."}, status=401)
            return

        if path == "/admin/api/logout":
            self._send_json({"ok": True}, extra_headers={"Set-Cookie": "aurum_session=; Path=/; Max-Age=0"})
            return

        # Protected Admin APIs below
        if not self._is_admin():
            self._send_json({"ok": False, "error": "Unauthorized"}, status=401)
            return

        if path == "/admin/api/notifications/read":
            nid = body.get("id")
            self.db.mark_notifications_read(int(nid) if nid is not None else None)
            self._send_json({"ok": True})
            return

        if path.startswith("/admin/api/orders/") and path.endswith("/approve"):
            ref = urllib.parse.unquote(path[len("/admin/api/orders/"):-len("/approve")]).strip("/")
            try:
                res = self.db.approve_order(ref, admin_note=str(body.get("admin_note", "")))
                lic = res["license"]
                email_res = self.mailer.send_license_delivery(
                    customer_name=lic["customer_name"],
                    customer_email=lic["customer_email"],
                    license_key=lic["license_key"],
                    mt5_login=lic["bound_mt5_login"],
                    plan_type=lic["plan_type"],
                    bot_names=lic["bot_names"],
                    expires_at=int(lic["expires_at"] or 0),
                    order_ref=ref,
                )
                self._send_json({"ok": True, "order": res["order"], "license": lic, "email_result": email_res})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            return

        if path.startswith("/admin/api/orders/") and path.endswith("/reject"):
            ref = urllib.parse.unquote(path[len("/admin/api/orders/"):-len("/reject")]).strip("/")
            try:
                ord_out = self.db.reject_order(ref, reason=str(body.get("reason", "")))
                self._send_json({"ok": True, "order": ord_out})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            return

        if path == "/admin/api/licenses":
            try:
                lic = self.db.create_manual_license(
                    customer_name=str(body.get("customer_name", "")),
                    customer_email=str(body.get("customer_email", "")),
                    mt5_login=str(body.get("mt5_login", "")),
                    plan_type=str(body.get("plan_type", "lifetime")),
                    bot_ids=list(body.get("bot_ids") or []),
                    amount_eur=float(body.get("amount_eur", 0) or 0),
                    notes=str(body.get("notes", "")),
                    days=int(body.get("days", 30) or 30),
                )
                email_res = None
                if body.get("send_email") and lic.get("customer_email"):
                    email_res = self.mailer.send_license_delivery(
                        customer_name=lic["customer_name"],
                        customer_email=lic["customer_email"],
                        license_key=lic["license_key"],
                        mt5_login=lic["bound_mt5_login"],
                        plan_type=lic["plan_type"],
                        bot_names=lic["bot_names"],
                        expires_at=int(lic["expires_at"] or 0),
                        order_ref="MANUAL",
                    )
                self._send_json({"ok": True, "license": lic, "email_result": email_res}, status=201)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            return

        if path.startswith("/admin/api/licenses/") and path.endswith("/resend"):
            key = urllib.parse.unquote(path[len("/admin/api/licenses/"):-len("/resend")]).strip("/")
            lic = self.db.get_license_by_key(key)
            if not lic:
                self._send_json({"ok": False, "error": "License not found"}, status=404)
                return
            email_res = self.mailer.send_license_delivery(
                customer_name=lic["customer_name"],
                customer_email=lic["customer_email"],
                license_key=lic["license_key"],
                mt5_login=lic["bound_mt5_login"],
                plan_type=lic["plan_type"],
                bot_names=lic["bot_names"],
                expires_at=int(lic["expires_at"] or 0),
                order_ref=lic.get("order_ref") or "RESEND",
            )
            self._send_json({
                "ok": bool(email_res.get("ok")),
                "recipient": lic["customer_email"],
                "error": email_res.get("error", ""),
                "email_result": email_res,
            })
            return

        if path.startswith("/admin/api/licenses/"):
            key = urllib.parse.unquote(path[len("/admin/api/licenses/"):]).strip("/")
            try:
                lic = self.db.update_license(
                    license_key=key,
                    status=body.get("status"),
                    bound_mt5_login=body.get("bound_mt5_login"),
                    bot_ids=body.get("bot_ids"),
                    extend_days=body.get("extend_days"),
                    make_lifetime=body.get("make_lifetime"),
                    notes=body.get("notes"),
                )
                self._send_json({"ok": True, "license": lic})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            return

        if path == "/admin/api/settings":
            allowed = {k: str(body[k]) for k in ("download_url", "admin_notify_email", "store_notice", "send_admin_copy") if k in body}
            updated = self.db.update_settings(allowed)
            self._send_json({"ok": True, "settings": updated})
            return

        if path == "/admin/api/settings/test-email":
            settings = self.db.get_settings()
            target = settings.get("admin_notify_email") or self.env_cfg["PUBLIC_CONTACT_EMAIL"]
            res = self.mailer.send_email(
                to_email=target,
                subject="Aurum Desk — Email Test Successful",
                text_body="Hello!\nYour Aurum Desk automatic email system is configured and working properly.",
                html_body="<p><strong>Hello!</strong></p><p>Your <strong>Aurum Desk</strong> automatic email system is configured and working properly.</p>",
            )
            self._send_json(res)
            return

        self._send_json({"ok": False, "error": "Unknown endpoint"}, status=404)


def create_app_server(port: Optional[int] = None, env_override: Optional[Dict[str, str]] = None, mailer_override: Optional[Mailer] = None) -> AurumHTTPServer:
    env_cfg = get_env_config()
    if env_override:
        env_cfg.update(env_override)
    bind_port = int(env_cfg["PORT"]) if port is None else int(port)

    db = Database(
        database_url=env_cfg["DATABASE_URL"],
        sqlite_path=env_cfg["SQLITE_PATH"],
        default_download_url=env_cfg["DEFAULT_DOWNLOAD_URL"],
        default_contact_email=env_cfg["PUBLIC_CONTACT_EMAIL"],
    )
    mailer = mailer_override if mailer_override is not None else Mailer(env_cfg, db=db)

    ui_dir = os.path.join(BASE_DIR, "ui")
    with open(os.path.join(ui_dir, "store.html"), "rb") as f:
        store_html = f.read()
    with open(os.path.join(ui_dir, "admin.html"), "rb") as f:
        admin_html = f.read()

    return AurumHTTPServer(("0.0.0.0", bind_port), AurumRequestHandler, db, mailer, env_cfg, store_html, admin_html)


def main() -> None:
    srv = create_app_server()
    port = srv.server_address[1]
    print(f"Aurum Desk Server listening on 0.0.0.0:{port} (Postgres={srv.db.is_postgres})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
