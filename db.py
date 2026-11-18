import contextlib
import json
import os
import secrets
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from config import BOT_BY_ID, PLAN_MONTHLY_DAYS, calculate_order_price_eur


def generate_unique_license_key() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    parts = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)]
    return f"AURUM-{parts[0]}-{parts[1]}-{parts[2]}"


def generate_order_ref() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    suffix = "".join(secrets.choice(alphabet) for _ in range(5))
    return f"AUR-{suffix}"


class Database:
    """Unified persistence layer supporting PostgreSQL (Neon/Render) and local SQLite."""

    def __init__(self, database_url: str = "", sqlite_path: str = "aurum_server.db", default_download_url: str = "", default_contact_email: str = "demajelio132@gmail.com"):
        self.database_url = (database_url or "").strip()
        self.sqlite_path = sqlite_path
        self.is_postgres = self.database_url.startswith("postgres://") or self.database_url.startswith("postgresql://")
        self._lock = threading.RLock()
        self._pg_mod = None

        if self.is_postgres:
            import psycopg
            from psycopg.rows import dict_row
            self._pg_mod = psycopg
            self._dict_row = dict_row
        else:
            parent = os.path.dirname(os.path.abspath(self.sqlite_path))
            if parent:
                os.makedirs(parent, exist_ok=True)

        self.init_schema(default_download_url=default_download_url, default_contact_email=default_contact_email)

    @contextlib.contextmanager
    def _connect(self):
        if self.is_postgres:
            conn = self._pg_mod.connect(self.database_url, row_factory=self._dict_row)
        else:
            conn = sqlite3.connect(self.sqlite_path, check_same_thread=False, timeout=20.0)
            conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _adapt_sql(self, sql: str) -> str:
        if self.is_postgres:
            return sql.replace("?", "%s")
        return sql

    def _fetch_all(self, conn, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        cur = conn.cursor()
        cur.execute(self._adapt_sql(sql), params)
        rows = cur.fetchall()
        return [dict(r) for r in rows]

    def _fetch_one(self, conn, sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
        cur = conn.cursor()
        cur.execute(self._adapt_sql(sql), params)
        row = cur.fetchone()
        return dict(row) if row is not None else None

    def _execute(self, conn, sql: str, params: tuple = ()) -> int:
        cur = conn.cursor()
        cur.execute(self._adapt_sql(sql), params)
        return cur.rowcount

    def init_schema(self, default_download_url: str = "", default_contact_email: str = "") -> None:
        id_col = "SERIAL PRIMARY KEY" if self.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
        stmts = [
            f"""
            CREATE TABLE IF NOT EXISTS orders (
                id {id_col},
                order_ref TEXT UNIQUE NOT NULL,
                customer_name TEXT NOT NULL,
                customer_email TEXT NOT NULL,
                mt5_login TEXT NOT NULL,
                plan_type TEXT NOT NULL,
                bundle_id TEXT DEFAULT '',
                bot_ids_json TEXT NOT NULL,
                amount_eur REAL NOT NULL,
                paypal_tx TEXT DEFAULT '',
                screenshot_data TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                admin_note TEXT DEFAULT '',
                license_key TEXT DEFAULT '',
                created_at INTEGER NOT NULL,
                reviewed_at INTEGER DEFAULT 0
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS licenses (
                id {id_col},
                license_key TEXT UNIQUE NOT NULL,
                customer_name TEXT NOT NULL,
                customer_email TEXT NOT NULL,
                bound_mt5_login TEXT NOT NULL,
                plan_type TEXT NOT NULL,
                bot_ids_json TEXT NOT NULL,
                amount_eur REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                expires_at INTEGER NOT NULL DEFAULT 0,
                order_ref TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                last_seen_at INTEGER NOT NULL DEFAULT 0,
                last_seen_ip TEXT DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS notifications (
                id {id_col},
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'info',
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                order_ref TEXT DEFAULT '',
                license_key TEXT DEFAULT '',
                is_read INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS email_logs (
                id {id_col},
                recipient TEXT NOT NULL,
                subject TEXT NOT NULL,
                status TEXT NOT NULL,
                provider TEXT DEFAULT '',
                error_text TEXT DEFAULT '',
                order_ref TEXT DEFAULT '',
                license_key TEXT DEFAULT '',
                created_at INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
        ]
        with self._lock:
            with self._connect() as conn:
                cur = conn.cursor()
                for s in stmts:
                    cur.execute(s)
                conn.commit()
                self._ensure_default_setting(conn, "download_url", default_download_url or "https://aurumdesk.com/download-coming-soon")
                self._ensure_default_setting(conn, "admin_notify_email", default_contact_email or "demajelio132@gmail.com")
                self._ensure_default_setting(conn, "send_admin_copy", "1")
                self._ensure_default_setting(conn, "store_notice", "")
                conn.commit()

    def _ensure_default_setting(self, conn, key: str, value: str) -> None:
        row = self._fetch_one(conn, "SELECT value FROM settings WHERE key = ?", (key,))
        if row is None:
            self._execute(conn, "INSERT INTO settings (key, value) VALUES (?, ?)", (key, str(value)))

    def get_settings(self) -> Dict[str, str]:
        with self._lock:
            with self._connect() as conn:
                rows = self._fetch_all(conn, "SELECT key, value FROM settings")
                return {r["key"]: r["value"] for r in rows}

    def update_settings(self, updates: Dict[str, str]) -> Dict[str, str]:
        with self._lock:
            with self._connect() as conn:
                for k, v in updates.items():
                    key = str(k).strip()
                    if not key:
                        continue
                    existing = self._fetch_one(conn, "SELECT key FROM settings WHERE key = ?", (key,))
                    if existing:
                        self._execute(conn, "UPDATE settings SET value = ? WHERE key = ?", (str(v), key))
                    else:
                        self._execute(conn, "INSERT INTO settings (key, value) VALUES (?, ?)", (key, str(v)))
                conn.commit()
                return self.get_settings()

    def add_notification(
        self,
        event_type: str,
        title: str,
        message: str,
        severity: str = "info",
        order_ref: str = "",
        license_key: str = "",
    ) -> Dict[str, Any]:
        now_ts = int(time.time())
        with self._lock:
            with self._connect() as conn:
                self._execute(
                    conn,
                    """
                    INSERT INTO notifications (event_type, severity, title, message, order_ref, license_key, is_read, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (event_type, severity, title, message, order_ref, license_key, now_ts),
                )
                conn.commit()
                row = self._fetch_one(conn, "SELECT * FROM notifications ORDER BY id DESC LIMIT 1")
                return row or {}

    def get_notifications(self, limit: int = 80) -> Dict[str, Any]:
        with self._lock:
            with self._connect() as conn:
                items = self._fetch_all(conn, "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (int(limit),))
                unread_row = self._fetch_one(conn, "SELECT COUNT(*) AS cnt FROM notifications WHERE is_read = 0")
                unread = int((unread_row or {}).get("cnt", 0))
                return {"items": items, "unread_count": unread}

    def mark_notifications_read(self, notif_id: Optional[int] = None) -> None:
        with self._lock:
            with self._connect() as conn:
                if notif_id is not None:
                    self._execute(conn, "UPDATE notifications SET is_read = 1 WHERE id = ?", (int(notif_id),))
                else:
                    self._execute(conn, "UPDATE notifications SET is_read = 1 WHERE is_read = 0")
                conn.commit()

    def log_email(
        self,
        recipient: str,
        subject: str,
        status: str,
        provider: str = "",
        error_text: str = "",
        order_ref: str = "",
        license_key: str = "",
    ) -> None:
        now_ts = int(time.time())
        with self._lock:
            with self._connect() as conn:
                self._execute(
                    conn,
                    """
                    INSERT INTO email_logs (recipient, subject, status, provider, error_text, order_ref, license_key, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (recipient, subject, status, provider, error_text, order_ref, license_key, now_ts),
                )
                conn.commit()

    def get_email_logs(self, limit: int = 60) -> List[Dict[str, Any]]:
        with self._lock:
            with self._connect() as conn:
                return self._fetch_all(conn, "SELECT * FROM email_logs ORDER BY id DESC LIMIT ?", (int(limit),))

    def create_order(
        self,
        customer_name: str,
        customer_email: str,
        mt5_login: str,
        plan_type: str,
        bot_ids: List[str],
        bundle_id: str = "",
        paypal_tx: str = "",
        screenshot_data: str = "",
    ) -> Dict[str, Any]:
        clean_bots = [b for b in dict.fromkeys(bot_ids) if b in BOT_BY_ID]
        if not clean_bots:
            raise ValueError("Please select at least one Expert Advisor.")
        mt5_clean = str(mt5_login or "").strip()
        if not mt5_clean.isdigit() or len(mt5_clean) < 3:
            raise ValueError("Please enter a valid numeric MT5 Account Number.")
        email_clean = str(customer_email or "").strip()
        if "@" not in email_clean or "." not in email_clean:
            raise ValueError("Please enter a valid email address.")
        name_clean = str(customer_name or "").strip()
        if len(name_clean) < 2:
            raise ValueError("Please enter your full name.")

        plan = "monthly" if str(plan_type).lower() in ("monthly", "30_days", "30days") else "lifetime"
        amount = float(calculate_order_price_eur(plan, clean_bots, bundle_id=bundle_id))
        now_ts = int(time.time())

        with self._lock:
            with self._connect() as conn:
                ref = generate_order_ref()
                while self._fetch_one(conn, "SELECT id FROM orders WHERE order_ref = ?", (ref,)):
                    ref = generate_order_ref()

                self._execute(
                    conn,
                    """
                    INSERT INTO orders (
                        order_ref, customer_name, customer_email, mt5_login, plan_type,
                        bundle_id, bot_ids_json, amount_eur, paypal_tx, screenshot_data,
                        status, created_at, reviewed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, 0)
                    """,
                    (
                        ref,
                        name_clean,
                        email_clean,
                        mt5_clean,
                        plan,
                        str(bundle_id or ""),
                        json.dumps(clean_bots),
                        amount,
                        str(paypal_tx or "").strip(),
                        str(screenshot_data or "").strip(),
                        now_ts,
                    ),
                )
                conn.commit()
                order = self._fetch_one(conn, "SELECT * FROM orders WHERE order_ref = ?", (ref,))

        ea_names = ", ".join(BOT_BY_ID[b]["name"] for b in clean_bots)
        plan_lbl = "30 Days" if plan == "monthly" else "Lifetime"
        self.add_notification(
            event_type="NEW_ORDER",
            severity="gold",
            title=f"New PayPal Order {ref} (€{int(amount)})",
            message=f"{name_clean} ({email_clean}) | MT5 #{mt5_clean} | {plan_lbl}: {ea_names} | TX: {paypal_tx or 'Screenshot/Pending Check'}",
            order_ref=ref,
        )
        return self._format_order(order)

    def _format_order(self, row: Optional[Dict[str, Any]], include_screenshot: bool = True) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        out = dict(row)
        try:
            out["bot_ids"] = json.loads(out.get("bot_ids_json") or "[]")
        except Exception:
            out["bot_ids"] = []
        out["bot_names"] = [BOT_BY_ID[b]["name"] for b in out["bot_ids"] if b in BOT_BY_ID]
        out["has_screenshot"] = bool(out.get("screenshot_data"))
        if not include_screenshot:
            out.pop("screenshot_data", None)
        return out

    def _format_license(self, row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        out = dict(row)
        try:
            out["bot_ids"] = json.loads(out.get("bot_ids_json") or "[]")
        except Exception:
            out["bot_ids"] = []
        out["bot_names"] = [BOT_BY_ID[b]["name"] for b in out["bot_ids"] if b in BOT_BY_ID]
        now_ts = int(time.time())
        exp = int(out.get("expires_at") or 0)
        out["is_expired"] = bool(exp > 0 and now_ts > exp)
        out["days_left"] = max(0, int((exp - now_ts + 86399) // 86400)) if exp > 0 else None
        return out

    def get_orders(self, status_filter: str = "all", include_screenshots: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            with self._connect() as conn:
                if status_filter and status_filter != "all":
                    rows = self._fetch_all(conn, "SELECT * FROM orders WHERE status = ? ORDER BY id DESC LIMIT 200", (status_filter,))
                else:
                    rows = self._fetch_all(conn, "SELECT * FROM orders ORDER BY id DESC LIMIT 200")
                return [self._format_order(r, include_screenshot=include_screenshots) for r in rows]

    def get_order_by_ref(self, order_ref: str, include_screenshot: bool = True) -> Optional[Dict[str, Any]]:
        with self._lock:
            with self._connect() as conn:
                row = self._fetch_one(conn, "SELECT * FROM orders WHERE order_ref = ?", (order_ref.strip().upper(),))
                return self._format_order(row, include_screenshot=include_screenshot)

    def approve_order(self, order_ref: str, admin_note: str = "") -> Dict[str, Any]:
        now_ts = int(time.time())
        ref = order_ref.strip().upper()
        with self._lock:
            with self._connect() as conn:
                order = self._fetch_one(conn, "SELECT * FROM orders WHERE order_ref = ?", (ref,))
                if not order:
                    raise ValueError(f"Order {ref} not found.")
                if order["status"] == "approved" and order.get("license_key"):
                    lic = self._fetch_one(conn, "SELECT * FROM licenses WHERE license_key = ?", (order["license_key"],))
                    return {"order": self._format_order(order), "license": self._format_license(lic), "already_approved": True}

                key = generate_unique_license_key()
                while self._fetch_one(conn, "SELECT id FROM licenses WHERE license_key = ?", (key,)):
                    key = generate_unique_license_key()

                plan = order["plan_type"]
                expires_at = (now_ts + PLAN_MONTHLY_DAYS * 86400) if plan == "monthly" else 0
                self._execute(
                    conn,
                    """
                    INSERT INTO licenses (
                        license_key, customer_name, customer_email, bound_mt5_login,
                        plan_type, bot_ids_json, amount_eur, status, expires_at,
                        order_ref, notes, last_seen_at, last_seen_ip, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, 0, '', ?, ?)
                    """,
                    (
                        key,
                        order["customer_name"],
                        order["customer_email"],
                        order["mt5_login"],
                        plan,
                        order["bot_ids_json"],
                        float(order["amount_eur"]),
                        expires_at,
                        ref,
                        admin_note,
                        now_ts,
                        now_ts,
                    ),
                )
                self._execute(
                    conn,
                    "UPDATE orders SET status = 'approved', admin_note = ?, license_key = ?, reviewed_at = ? WHERE order_ref = ?",
                    (admin_note, key, now_ts, ref),
                )
                conn.commit()
                updated_order = self._fetch_one(conn, "SELECT * FROM orders WHERE order_ref = ?", (ref,))
                lic_row = self._fetch_one(conn, "SELECT * FROM licenses WHERE license_key = ?", (key,))

        self.add_notification(
            event_type="ORDER_APPROVED",
            severity="success",
            title=f"Order Approved: {ref} → {key}",
            message=f"Customer: {order['customer_name']} ({order['customer_email']}) | MT5 #{order['mt5_login']} | Key: {key}",
            order_ref=ref,
            license_key=key,
        )
        return {
            "order": self._format_order(updated_order),
            "license": self._format_license(lic_row),
            "already_approved": False,
        }

    def reject_order(self, order_ref: str, reason: str = "") -> Dict[str, Any]:
        now_ts = int(time.time())
        ref = order_ref.strip().upper()
        with self._lock:
            with self._connect() as conn:
                order = self._fetch_one(conn, "SELECT * FROM orders WHERE order_ref = ?", (ref,))
                if not order:
                    raise ValueError(f"Order {ref} not found.")
                self._execute(
                    conn,
                    "UPDATE orders SET status = 'rejected', admin_note = ?, reviewed_at = ? WHERE order_ref = ?",
                    (reason or "Payment not verified", now_ts, ref),
                )
                conn.commit()
                updated = self._fetch_one(conn, "SELECT * FROM orders WHERE order_ref = ?", (ref,))
        self.add_notification(
            event_type="ORDER_REJECTED",
            severity="warn",
            title=f"Order Rejected: {ref}",
            message=f"Reason: {reason or 'Payment not verified'}",
            order_ref=ref,
        )
        return self._format_order(updated)

    def create_manual_license(
        self,
        customer_name: str,
        customer_email: str,
        mt5_login: str,
        plan_type: str,
        bot_ids: List[str],
        amount_eur: float = 0.0,
        notes: str = "",
        days: int = PLAN_MONTHLY_DAYS,
    ) -> Dict[str, Any]:
        clean_bots = [b for b in dict.fromkeys(bot_ids) if b in BOT_BY_ID]
        if not clean_bots:
            raise ValueError("Select at least one EA.")
        mt5_clean = str(mt5_login or "").strip()
        if not mt5_clean.isdigit() or len(mt5_clean) < 3:
            raise ValueError("Enter a valid numeric MT5 Account Number.")
        plan = "monthly" if str(plan_type).lower() in ("monthly", "30_days", "30days") else "lifetime"
        now_ts = int(time.time())
        expires_at = (now_ts + max(1, int(days)) * 86400) if plan == "monthly" else 0

        with self._lock:
            with self._connect() as conn:
                key = generate_unique_license_key()
                while self._fetch_one(conn, "SELECT id FROM licenses WHERE license_key = ?", (key,)):
                    key = generate_unique_license_key()
                self._execute(
                    conn,
                    """
                    INSERT INTO licenses (
                        license_key, customer_name, customer_email, bound_mt5_login,
                        plan_type, bot_ids_json, amount_eur, status, expires_at,
                        order_ref, notes, last_seen_at, last_seen_ip, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, 'MANUAL', ?, 0, '', ?, ?)
                    """,
                    (
                        key,
                        customer_name.strip() or "Customer",
                        customer_email.strip(),
                        mt5_clean,
                        plan,
                        json.dumps(clean_bots),
                        float(amount_eur or 0.0),
                        expires_at,
                        notes,
                        now_ts,
                        now_ts,
                    ),
                )
                conn.commit()
                row = self._fetch_one(conn, "SELECT * FROM licenses WHERE license_key = ?", (key,))
        self.add_notification(
            event_type="MANUAL_KEY",
            severity="info",
            title=f"Manual License Created: {key}",
            message=f"{customer_name} ({customer_email}) | MT5 #{mt5_clean} | {', '.join(clean_bots)}",
            license_key=key,
        )
        return self._format_license(row)

    def get_licenses(self, search: str = "") -> List[Dict[str, Any]]:
        with self._lock:
            with self._connect() as conn:
                q = search.strip().lower()
                rows = self._fetch_all(conn, "SELECT * FROM licenses ORDER BY id DESC LIMIT 300")
                formatted = [self._format_license(r) for r in rows]
                if q:
                    formatted = [
                        x for x in formatted
                        if q in x["license_key"].lower()
                        or q in x["customer_name"].lower()
                        or q in x["customer_email"].lower()
                        or q in str(x["bound_mt5_login"]).lower()
                    ]
                return formatted

    def get_license_by_key(self, license_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            with self._connect() as conn:
                row = self._fetch_one(conn, "SELECT * FROM licenses WHERE license_key = ?", (license_key.strip().upper(),))
                return self._format_license(row)

    def update_license(
        self,
        license_key: str,
        status: Optional[str] = None,
        bound_mt5_login: Optional[str] = None,
        bot_ids: Optional[List[str]] = None,
        extend_days: Optional[int] = None,
        make_lifetime: Optional[bool] = None,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        key = license_key.strip().upper()
        now_ts = int(time.time())
        with self._lock:
            with self._connect() as conn:
                row = self._fetch_one(conn, "SELECT * FROM licenses WHERE license_key = ?", (key,))
                if not row:
                    raise ValueError("License key not found.")

                new_status = status if status in ("active", "suspended", "revoked") else row["status"]
                new_mt5 = row["bound_mt5_login"]
                if bound_mt5_login is not None:
                    mt5_clean = str(bound_mt5_login).strip()
                    if not mt5_clean.isdigit() or len(mt5_clean) < 3:
                        raise ValueError("MT5 Account Number must be numeric.")
                    new_mt5 = mt5_clean

                new_bots_json = row["bot_ids_json"]
                if bot_ids is not None:
                    clean_bots = [b for b in dict.fromkeys(bot_ids) if b in BOT_BY_ID]
                    if not clean_bots:
                        raise ValueError("At least one EA must remain assigned.")
                    new_bots_json = json.dumps(clean_bots)

                new_plan = row["plan_type"]
                new_exp = int(row["expires_at"] or 0)
                if make_lifetime:
                    new_plan = "lifetime"
                    new_exp = 0
                elif extend_days and int(extend_days) > 0:
                    new_plan = "monthly"
                    base_ts = max(now_ts, new_exp) if new_exp > 0 else now_ts
                    new_exp = base_ts + int(extend_days) * 86400

                new_notes = row["notes"] if notes is None else str(notes)

                self._execute(
                    conn,
                    """
                    UPDATE licenses
                    SET status = ?, bound_mt5_login = ?, bot_ids_json = ?,
                        plan_type = ?, expires_at = ?, notes = ?, updated_at = ?
                    WHERE license_key = ?
                    """,
                    (new_status, new_mt5, new_bots_json, new_plan, new_exp, new_notes, now_ts, key),
                )
                conn.commit()
                updated = self._fetch_one(conn, "SELECT * FROM licenses WHERE license_key = ?", (key,))
        return self._format_license(updated)

    def verify_license(self, license_key: str, mt5_login: str = "", client_ip: str = "") -> Dict[str, Any]:
        key = str(license_key or "").strip().upper()
        if not key:
            return {"ok": False, "error": "missing_key", "message": "Please enter your Unique License Key."}

        now_ts = int(time.time())
        with self._lock:
            with self._connect() as conn:
                row = self._fetch_one(conn, "SELECT * FROM licenses WHERE license_key = ?", (key,))
                if not row:
                    return {"ok": False, "error": "invalid_key", "message": "Invalid Unique License Key."}

                lic = self._format_license(row)
                if lic["status"] != "active":
                    return {
                        "ok": False,
                        "error": f"license_{lic['status']}",
                        "message": f"This license is currently {lic['status']}. Contact support ({self.get_settings().get('admin_notify_email', '')}).",
                    }

                if lic["is_expired"]:
                    return {
                        "ok": False,
                        "error": "expired",
                        "message": "Your 30-day license has expired. Please renew to continue trading.",
                    }

                mt5_clean = str(mt5_login or "").strip()
                bound = str(lic["bound_mt5_login"]).strip()

                # Update last_seen telemetry
                self._execute(
                    conn,
                    "UPDATE licenses SET last_seen_at = ?, last_seen_ip = ? WHERE license_key = ?",
                    (now_ts, client_ip[:64], key),
                )
                conn.commit()

        if mt5_clean and mt5_clean != "0":
            if mt5_clean != bound:
                self.add_notification(
                    event_type="MT5_MISMATCH",
                    severity="danger",
                    title=f"Blocked Unauthorized MT5 Account (#{mt5_clean})",
                    message=(
                        f"Key {key} ({lic['customer_name']}) is locked to MT5 #{bound}, "
                        f"but someone tried using MT5 #{mt5_clean} from IP {client_ip or 'unknown'}."
                    ),
                    license_key=key,
                )
                return {
                    "ok": False,
                    "error": "account_mismatch",
                    "bound_mt5_login": bound,
                    "message": (
                        f"Access Denied: This Unique Key is locked to MT5 Account #{bound}, "
                        f"but MetaTrader 5 is connected to #{mt5_clean}."
                    ),
                }
            return {
                "ok": True,
                "mt5_verified": True,
                "license_key": key,
                "customer_name": lic["customer_name"],
                "bound_mt5_login": bound,
                "plan_type": lic["plan_type"],
                "expires_at": lic["expires_at"],
                "days_left": lic["days_left"],
                "allowed_bots": lic["bot_ids"],
                "allowed_bot_names": lic["bot_names"],
                "message": f"License active for MT5 #{bound} ({len(lic['bot_ids'])} EA unlocked).",
            }

        # MT5 not yet connected on desktop client
        return {
            "ok": True,
            "mt5_verified": False,
            "license_key": key,
            "customer_name": lic["customer_name"],
            "bound_mt5_login": bound,
            "plan_type": lic["plan_type"],
            "expires_at": lic["expires_at"],
            "days_left": lic["days_left"],
            "allowed_bots": lic["bot_ids"],
            "allowed_bot_names": lic["bot_names"],
            "message": f"Key valid for MT5 #{bound}. Waiting for MT5 account #{bound} connection...",
        }

    def get_dashboard_metrics(self) -> Dict[str, Any]:
        with self._lock:
            with self._connect() as conn:
                orders = self._fetch_all(conn, "SELECT status, amount_eur FROM orders")
                licenses = self._fetch_all(conn, "SELECT status, expires_at, amount_eur FROM licenses")
                now_ts = int(time.time())
                pending_orders = sum(1 for o in orders if o["status"] == "pending")
                approved_orders = sum(1 for o in orders if o["status"] == "approved")
                total_revenue = round(sum(float(o["amount_eur"] or 0) for o in orders if o["status"] == "approved"), 2)
                active_licenses = sum(
                    1 for l in licenses
                    if l["status"] == "active" and (int(l["expires_at"] or 0) == 0 or int(l["expires_at"]) > now_ts)
                )
                return {
                    "pending_orders": pending_orders,
                    "approved_orders": approved_orders,
                    "active_licenses": active_licenses,
                    "total_licenses": len(licenses),
                    "total_revenue_eur": total_revenue,
                }
