import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
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


def sign_customer_token(secret: str, user_id: int, token_version: int, issued_at: int) -> str:
    payload = f"aurum_cust:{int(user_id)}:{int(token_version)}:{int(issued_at)}"
    sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    raw = f"{payload}:{sig}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def verify_customer_token(token: str, secret: str, max_age_sec: int = 86400 * 30) -> Optional[Dict[str, int]]:
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        parts = raw.split(":")
        if len(parts) != 5 or parts[0] != "aurum_cust":
            return None
        user_id = int(parts[1])
        token_version = int(parts[2])
        issued_at = int(parts[3])
        if time.time() - issued_at > max_age_sec:
            return None
        payload = f"{parts[0]}:{parts[1]}:{parts[2]}:{parts[3]}"
        expected = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(parts[4], expected):
            return None
        return {"user_id": user_id, "token_version": token_version}
    except Exception:
        return None


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
    server_version = "AurumDeskServer/1.1"

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

    def _get_cookie(self, name: str) -> str:
        cookie_hdr = self.headers.get("Cookie", "")
        if not cookie_hdr:
            return ""
        c = SimpleCookie()
        try:
            c.load(cookie_hdr)
        except Exception:
            return ""
        morsel = c.get(name)
        return morsel.value if morsel else ""

    def _is_admin(self) -> bool:
        token = self._get_cookie("aurum_session")
        return verify_session_token(token, self.env_cfg["SECRET_KEY"])

    def _get_customer(self) -> Optional[Dict[str, Any]]:
        token = self._get_cookie("aurum_customer_session")
        parsed = verify_customer_token(token, self.env_cfg["SECRET_KEY"])
        if not parsed:
            return None
        user = self.db.get_customer_by_id(parsed["user_id"])
        if not user:
            return None
        if user.get("status") != "active" or not user.get("email_verified"):
            return None
        if int(user.get("token_version") or 1) != parsed["token_version"]:
            return None
        return user

    def _effective_paypal_url(self, settings: Optional[Dict[str, str]] = None) -> str:
        s = settings if settings is not None else self.db.get_settings()
        raw = (s.get("paypal_me_url") or "").strip() or self.env_cfg.get("PAYPAL_ME_URL", "https://paypal.me/AurmDesk")
        raw = raw.strip().rstrip("/")
        if raw and not raw.lower().startswith(("http://", "https://")):
            raw = "https://" + raw.lstrip("/")
        return raw

    def _customer_cookie_header(self, user: Dict[str, Any]) -> str:
        token = sign_customer_token(
            self.env_cfg["SECRET_KEY"],
            int(user["id"]),
            int(user.get("token_version") or 1),
            int(time.time()),
        )
        sec_flag = "; Secure" if self._is_https() else ""
        return f"aurum_customer_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={86400 * 30}{sec_flag}"

    def _send_html(self, data: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
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
        if self.command != "HEAD":
            self.wfile.write(raw)

    def do_HEAD(self) -> None:
        self.do_GET()

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

        if path in ("/", "/index.html", "/account", "/account/"):
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
                "paypal_me_url": self._effective_paypal_url(settings),
                "contact_email": settings.get("admin_notify_email") or self.env_cfg["PUBLIC_CONTACT_EMAIL"],
                "store_notice": settings.get("store_notice", ""),
            })
            return

        if path == "/api/auth/me":
            user = self._get_customer()
            self._send_json({"ok": True, "authenticated": bool(user), "user": user})
            return

        if path == "/api/account/overview":
            user = self._get_customer()
            if not user:
                self._send_json({"ok": False, "error": "Please sign in to view your account."}, status=401)
                return
            ov = self.db.get_customer_overview(int(user["id"]))
            settings = self.db.get_settings()
            dl_url = ""
            if ov.get("has_active_license"):
                dl_url = settings.get("download_url") or self.env_cfg["DEFAULT_DOWNLOAD_URL"]
            ov["download_url"] = dl_url
            ov["ok"] = True
            self._send_json(ov)
            return

        if path.startswith("/api/order-status/"):
            ref = urllib.parse.unquote(path.split("/api/order-status/", 1)[1]).strip()
            order = self.db.get_order_by_ref(ref, include_screenshot=False)
            if not order:
                self._send_json({"ok": False, "error": "Order reference not found."}, status=404)
                return
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
            settings = self.db.get_settings()
            self._send_json({
                "ok": True,
                "bots": BOT_CATALOG,
                "orders": self.db.get_orders(status_filter="all", include_screenshots=False),
                "licenses": self.db.get_licenses(),
                "mt5_requests": self.db.get_mt5_requests(status_filter="all"),
                "users": self.db.get_users(),
                "notifications": notif_data["items"],
                "unread_count": notif_data["unread_count"],
                "email_logs": self.db.get_email_logs(limit=60),
                "settings": settings,
                "effective_paypal_me_url": self._effective_paypal_url(settings),
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

        # ================= CUSTOMER AUTH & PORTAL =================
        if path == "/api/auth/register":
            try:
                reg = self.db.register_customer(
                    full_name=str(body.get("full_name", "")),
                    email=str(body.get("email", "")),
                    password=str(body.get("password", "")),
                )
                user = reg["user"]
                mail_res = self.mailer.send_verification_email(
                    customer_name=user["full_name"],
                    customer_email=user["email"],
                    code=reg["code"],
                    purpose="register",
                )
                self._send_json(
                    {
                        "ok": True,
                        "verification_required": True,
                        "email": user["email"],
                        "email_sent": bool(mail_res.get("ok")),
                        "email_error": "" if mail_res.get("ok") else mail_res.get("error", ""),
                    },
                    status=201,
                )
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            return

        if path == "/api/auth/resend-code":
            try:
                res = self.db.resend_verification_code(email=str(body.get("email", "")))
                user = res["user"]
                mail_res = self.mailer.send_verification_email(
                    customer_name=user["full_name"],
                    customer_email=user["email"],
                    code=res["code"],
                    purpose="register",
                )
                self._send_json({
                    "ok": bool(mail_res.get("ok")),
                    "email": user["email"],
                    "error": "" if mail_res.get("ok") else mail_res.get("error", "Could not send verification email."),
                }, status=200 if mail_res.get("ok") else 500)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            return

        if path == "/api/auth/verify-email":
            try:
                user = self.db.verify_customer_email(
                    email=str(body.get("email", "")),
                    code=str(body.get("code", "")),
                )
                cookie = self._customer_cookie_header(user)
                self._send_json({"ok": True, "user": user}, extra_headers={"Set-Cookie": cookie})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            return

        if path == "/api/auth/login":
            email_raw = str(body.get("email", ""))
            password_raw = str(body.get("password", ""))
            try:
                user = self.db.authenticate_customer(email=email_raw, password=password_raw)
                cookie = self._customer_cookie_header(user)
                self._send_json({"ok": True, "user": user}, extra_headers={"Set-Cookie": cookie})
            except PermissionError:
                self._send_json(
                    {
                        "ok": False,
                        "error": "Your email is not verified yet. Enter your 6-digit verification code or resend a new code.",
                        "verification_required": True,
                        "email": email_raw.strip().lower(),
                    },
                    status=403,
                )
            except Exception as exc:
                time.sleep(0.25)
                self._send_json({"ok": False, "error": str(exc)}, status=401)
            return

        if path == "/api/auth/logout":
            self._send_json(
                {"ok": True},
                extra_headers={"Set-Cookie": "aurum_customer_session=; Path=/; Max-Age=0"},
            )
            return

        if path == "/api/auth/forgot-password":
            try:
                res = self.db.request_password_reset(email=str(body.get("email", "")))
                if res:
                    user = res["user"]
                    self.mailer.send_verification_email(
                        customer_name=user["full_name"],
                        customer_email=user["email"],
                        code=res["code"],
                        purpose="reset",
                    )
                self._send_json({
                    "ok": True,
                    "message": "If an active verified account exists for that email, a 6-digit reset code has been sent.",
                })
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            return

        if path == "/api/auth/reset-password":
            try:
                user = self.db.reset_password_with_code(
                    email=str(body.get("email", "")),
                    code=str(body.get("code", "")),
                    new_password=str(body.get("new_password", "")),
                )
                cookie = self._customer_cookie_header(user)
                self._send_json({"ok": True, "user": user}, extra_headers={"Set-Cookie": cookie})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            return

        if path == "/api/account/profile":
            user = self._get_customer()
            if not user:
                self._send_json({"ok": False, "error": "Please sign in first."}, status=401)
                return
            try:
                updated = self.db.update_customer_profile(
                    user_id=int(user["id"]),
                    full_name=str(body.get("full_name", user["full_name"])),
                    current_password=str(body.get("current_password", "")),
                    new_password=str(body.get("new_password", "")),
                )
                cookie = self._customer_cookie_header(updated)
                self._send_json({"ok": True, "user": updated}, extra_headers={"Set-Cookie": cookie})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            return

        if path == "/api/account/mt5-switch":
            user = self._get_customer()
            if not user:
                self._send_json({"ok": False, "error": "Please sign in first."}, status=401)
                return
            try:
                req_row = self.db.create_mt5_switch_request(
                    user_id=int(user["id"]),
                    license_key=str(body.get("license_key", "")),
                    new_mt5_login=str(body.get("new_mt5_login", "")),
                    reason=str(body.get("reason", "")),
                )
                self.mailer.send_admin_alert_email(
                    subject=f"[Aurum Desk] MT5 Switch Request: {req_row['license_key']}",
                    message=(
                        f"Customer: {req_row['customer_name']} ({req_row['customer_email']})\n"
                        f"License Key: {req_row['license_key']}\n"
                        f"From MT5 #{req_row['old_mt5_login']} -> To MT5 #{req_row['new_mt5_login']}\n"
                        f"Reason: {req_row.get('reason') or 'Not specified'}\n\n"
                        f"Open /admin to approve or decline."
                    ),
                    license_key=req_row["license_key"],
                )
                self._send_json({"ok": True, "request": req_row}, status=201)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            return

        # 1) Customer order submission (Requires verified signed-in customer account)
        if path == "/api/orders":
            user = self._get_customer()
            if not user:
                self._send_json(
                    {
                        "ok": False,
                        "error": "Please sign in or create a verified account before placing an order.",
                        "auth_required": True,
                    },
                    status=401,
                )
                return
            try:
                cust_name = str(body.get("customer_name") or user["full_name"]).strip() or user["full_name"]
                order = self.db.create_order(
                    customer_name=cust_name,
                    customer_email=user["email"],
                    mt5_login=str(body.get("mt5_login", "")),
                    plan_type=str(body.get("plan_type", "lifetime")),
                    bot_ids=list(body.get("bot_ids") or []),
                    bundle_id=str(body.get("bundle_id", "")),
                    paypal_tx=str(body.get("paypal_tx", "")),
                    screenshot_data=str(body.get("screenshot_data", "")),
                    user_id=int(user["id"]),
                )
                self.mailer.send_admin_alert_email(
                    subject=f"[Aurum Desk] New PayPal Order {order['order_ref']} (€{int(order['amount_eur'])})",
                    message=(
                        f"Order Ref: {order['order_ref']}\n"
                        f"Customer: {order['customer_name']} ({order['customer_email']})\n"
                        f"MT5 Account: #{order['mt5_login']}\n"
                        f"Plan: {order['plan_type']}\n"
                        f"EAs: {', '.join(order.get('bot_names') or [])}\n"
                        f"Amount: €{order['amount_eur']}\n"
                        f"PayPal TX: {order.get('paypal_tx') or 'Pending check'}\n\n"
                        f"Open /admin to verify payment and approve."
                    ),
                    order_ref=order["order_ref"],
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

        if path.startswith("/admin/api/mt5-requests/") and path.endswith("/approve"):
            raw_id = path[len("/admin/api/mt5-requests/"):-len("/approve")].strip("/")
            try:
                res = self.db.approve_mt5_switch_request(int(raw_id), admin_note=str(body.get("admin_note", "")))
                req_row = res["request"]
                mail_res = self.mailer.send_mt5_switch_notification(
                    customer_name=req_row["customer_name"],
                    customer_email=req_row["customer_email"],
                    license_key=req_row["license_key"],
                    old_mt5=req_row["old_mt5_login"],
                    new_mt5=req_row["new_mt5_login"],
                    approved=True,
                    note=req_row.get("admin_note", ""),
                )
                self._send_json({"ok": True, "request": req_row, "license": res["license"], "email_result": mail_res})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            return

        if path.startswith("/admin/api/mt5-requests/") and path.endswith("/reject"):
            raw_id = path[len("/admin/api/mt5-requests/"):-len("/reject")].strip("/")
            try:
                req_row = self.db.reject_mt5_switch_request(int(raw_id), reason=str(body.get("reason", "")))
                mail_res = self.mailer.send_mt5_switch_notification(
                    customer_name=req_row["customer_name"],
                    customer_email=req_row["customer_email"],
                    license_key=req_row["license_key"],
                    old_mt5=req_row["old_mt5_login"],
                    new_mt5=req_row["new_mt5_login"],
                    approved=False,
                    note=req_row.get("admin_note", ""),
                )
                self._send_json({"ok": True, "request": req_row, "email_result": mail_res})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            return

        if path.startswith("/admin/api/users/") and path.endswith("/status"):
            raw_id = path[len("/admin/api/users/"):-len("/status")].strip("/")
            try:
                user_out = self.db.update_user_status(int(raw_id), status=str(body.get("status", "")))
                self._send_json({"ok": True, "user": user_out})
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
            allowed = {k: str(body[k]) for k in ("download_url", "admin_notify_email", "store_notice", "send_admin_copy", "paypal_me_url") if k in body}
            if "paypal_me_url" in allowed:
                pp = allowed["paypal_me_url"].strip().rstrip("/")
                if pp and not pp.lower().startswith(("http://", "https://")):
                    pp = "https://" + pp.lstrip("/")
                allowed["paypal_me_url"] = pp
            updated = self.db.update_settings(allowed)
            self._send_json({"ok": True, "settings": updated, "effective_paypal_me_url": self._effective_paypal_url(updated)})
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
        secret_key=env_cfg["SECRET_KEY"],
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
