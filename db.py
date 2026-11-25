import contextlib
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from config import (
    BOT_BY_ID,
    OFFICIAL_DOWNLOAD_URL,
    PLACEHOLDER_DOWNLOAD_URLS,
    PLAN_MONTHLY_DAYS,
    calculate_order_price_eur,
    resolve_download_url,
)


def generate_unique_license_key() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    parts = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)]
    return f"AURUM-{parts[0]}-{parts[1]}-{parts[2]}"


def generate_order_ref() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    suffix = "".join(secrets.choice(alphabet) for _ in range(5))
    return f"AUR-{suffix}"


def generate_6digit_code() -> str:
    return str(100_000 + secrets.randbelow(900_000))


def hash_password(password: str) -> str:
    pwd = str(password or "")
    if len(pwd) < 8:
        raise ValueError("Password must be at least 8 characters long.")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt, 240_000)
    return f"pbkdf2_sha256$240000${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        parts = str(stored_hash or "").split("$")
        if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
            return False
        iters = int(parts[1])
        salt = bytes.fromhex(parts[2])
        expected = bytes.fromhex(parts[3])
        dk = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt, iters)
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


class Database:
    """Unified persistence layer supporting PostgreSQL (Neon/Render) and local SQLite."""

    def __init__(
        self,
        database_url: str = "",
        sqlite_path: str = "aurum_server.db",
        default_download_url: str = "",
        default_contact_email: str = "eliodemaj07@gmail.com",
        secret_key: str = "aurum-default-secret",
    ):
        self.database_url = (database_url or "").strip()
        self.sqlite_path = sqlite_path
        self.secret_key = (secret_key or "aurum-default-secret").strip()
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

    def _ensure_column(self, conn, table_name: str, column_name: str, column_def: str) -> None:
        if self.is_postgres:
            row = self._fetch_one(
                conn,
                "SELECT column_name FROM information_schema.columns WHERE table_name = ? AND column_name = ?",
                (table_name, column_name),
            )
            exists = row is not None
        else:
            cols = self._fetch_all(conn, f"PRAGMA table_info({table_name})")
            exists = any(c.get("name") == column_name for c in cols)
        if not exists:
            self._execute(conn, f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")

    def init_schema(self, default_download_url: str = "", default_contact_email: str = "") -> None:
        id_col = "SERIAL PRIMARY KEY" if self.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
        stmts = [
            f"""
            CREATE TABLE IF NOT EXISTS users (
                id {id_col},
                full_name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                email_verified INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                token_version INTEGER NOT NULL DEFAULT 1,
                last_login_at INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS verification_codes (
                id {id_col},
                email TEXT NOT NULL,
                purpose TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                expires_at INTEGER NOT NULL,
                used_at INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS mt5_requests (
                id {id_col},
                user_id INTEGER NOT NULL,
                customer_email TEXT NOT NULL,
                customer_name TEXT NOT NULL,
                license_key TEXT NOT NULL,
                old_mt5_login TEXT NOT NULL,
                new_mt5_login TEXT NOT NULL,
                reason TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                admin_note TEXT DEFAULT '',
                created_at INTEGER NOT NULL,
                reviewed_at INTEGER NOT NULL DEFAULT 0
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS orders (
                id {id_col},
                order_ref TEXT UNIQUE NOT NULL,
                user_id INTEGER NOT NULL DEFAULT 0,
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
                user_id INTEGER NOT NULL DEFAULT 0,
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
            """
            CREATE TABLE IF NOT EXISTS growth_publish (
                id INTEGER PRIMARY KEY,
                snapshot_json TEXT NOT NULL,
                meta_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """,
        ]
        with self._lock:
            with self._connect() as conn:
                cur = conn.cursor()
                for s in stmts:
                    cur.execute(s)
                conn.commit()
                # Safe migrations for pre-existing tables on Neon / SQLite
                self._ensure_column(conn, "orders", "user_id", "INTEGER NOT NULL DEFAULT 0")
                self._ensure_column(conn, "licenses", "user_id", "INTEGER NOT NULL DEFAULT 0")
                resolved_dl = resolve_download_url(default_download_url or OFFICIAL_DOWNLOAD_URL)
                self._ensure_default_setting(conn, "download_url", resolved_dl)
                cur_dl_row = self._fetch_one(conn, "SELECT value FROM settings WHERE key = ?", ("download_url",))
                if cur_dl_row and str(cur_dl_row.get("value") or "").strip() in PLACEHOLDER_DOWNLOAD_URLS:
                    self._execute(conn, "UPDATE settings SET value = ? WHERE key = ?", (resolved_dl, "download_url"))
                self._ensure_default_setting(conn, "admin_notify_email", default_contact_email or "eliodemaj07@gmail.com")
                self._ensure_default_setting(conn, "send_admin_copy", "1")
                self._ensure_default_setting(conn, "store_notice", "")
                self._ensure_default_setting(conn, "paypal_me_url", "")
                self._ensure_default_setting(conn, "growth_sync_token", f"ADG-{secrets.token_urlsafe(24)}")
                self._ensure_default_setting(conn, "growth_sync_mt5_login", "")
                conn.commit()

    def _ensure_default_setting(self, conn, key: str, value: str) -> None:
        row = self._fetch_one(conn, "SELECT value FROM settings WHERE key = ?", (key,))
        if row is None:
            self._execute(conn, "INSERT INTO settings (key, value) VALUES (?, ?)", (key, str(value)))

    def get_settings(self) -> Dict[str, str]:
        with self._lock:
            with self._connect() as conn:
                rows = self._fetch_all(conn, "SELECT key, value FROM settings")
                out = {r["key"]: r["value"] for r in rows}
                out["download_url"] = resolve_download_url(out.get("download_url", ""))
                return out

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

    # ================= CUSTOMER ACCOUNTS & AUTH =================

    def _norm_email(self, email: str) -> str:
        clean = str(email or "").strip().lower()
        if not clean or "@" not in clean or "." not in clean.split("@")[-1] or " " in clean:
            raise ValueError("Please enter a valid email address.")
        return clean

    def _hash_code(self, email_norm: str, purpose: str, code: str) -> str:
        msg = f"{email_norm}:{purpose}:{code.strip()}".encode("utf-8")
        return hmac.new(self.secret_key.encode("utf-8"), msg, hashlib.sha256).hexdigest()

    def _format_user(self, row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        out = dict(row)
        out.pop("password_hash", None)
        out["email_verified"] = bool(int(out.get("email_verified") or 0))
        return out

    def _link_customer_records_conn(self, conn, user_id: int, email_norm: str) -> None:
        self._execute(
            conn,
            "UPDATE orders SET user_id = ? WHERE user_id = 0 AND LOWER(TRIM(customer_email)) = ?",
            (int(user_id), email_norm),
        )
        self._execute(
            conn,
            "UPDATE licenses SET user_id = ? WHERE user_id = 0 AND LOWER(TRIM(customer_email)) = ?",
            (int(user_id), email_norm),
        )

    def register_customer(self, full_name: str, email: str, password: str) -> Dict[str, Any]:
        name_clean = str(full_name or "").strip()
        if len(name_clean) < 2:
            raise ValueError("Please enter your full name (at least 2 characters).")
        email_norm = self._norm_email(email)
        pwd_hash = hash_password(password)
        now_ts = int(time.time())
        code = generate_6digit_code()
        code_hash = self._hash_code(email_norm, "register", code)

        with self._lock:
            with self._connect() as conn:
                existing = self._fetch_one(conn, "SELECT * FROM users WHERE email = ?", (email_norm,))
                if existing:
                    if int(existing.get("email_verified") or 0) == 1:
                        raise ValueError("An account with this email already exists. Please sign in instead.")
                    if existing.get("status") != "active":
                        raise ValueError("This account is disabled. Please contact support.")
                    self._execute(
                        conn,
                        "UPDATE users SET full_name = ?, password_hash = ?, updated_at = ? WHERE id = ?",
                        (name_clean, pwd_hash, now_ts, int(existing["id"])),
                    )
                else:
                    self._execute(
                        conn,
                        """
                        INSERT INTO users (
                            full_name, email, password_hash, email_verified, status,
                            token_version, last_login_at, created_at, updated_at
                        ) VALUES (?, ?, ?, 0, 'active', 1, 0, ?, ?)
                        """,
                        (name_clean, email_norm, pwd_hash, now_ts, now_ts),
                    )

                self._execute(
                    conn,
                    "UPDATE verification_codes SET used_at = ? WHERE email = ? AND purpose = 'register' AND used_at = 0",
                    (now_ts, email_norm),
                )
                self._execute(
                    conn,
                    """
                    INSERT INTO verification_codes (email, purpose, code_hash, attempts, expires_at, used_at, created_at)
                    VALUES (?, 'register', ?, 0, ?, 0, ?)
                    """,
                    (email_norm, code_hash, now_ts + 900, now_ts),
                )
                conn.commit()
                user_row = self._fetch_one(conn, "SELECT * FROM users WHERE email = ?", (email_norm,))

        return {"user": self._format_user(user_row), "code": code}

    def resend_verification_code(self, email: str) -> Dict[str, Any]:
        email_norm = self._norm_email(email)
        now_ts = int(time.time())
        with self._lock:
            with self._connect() as conn:
                user_row = self._fetch_one(conn, "SELECT * FROM users WHERE email = ?", (email_norm,))
                if not user_row:
                    raise ValueError("No account found for this email.")
                if int(user_row.get("email_verified") or 0) == 1:
                    raise ValueError("This email is already verified. Please sign in.")
                if user_row.get("status") != "active":
                    raise ValueError("This account is disabled. Please contact support.")

                latest = self._fetch_one(
                    conn,
                    "SELECT * FROM verification_codes WHERE email = ? AND purpose = 'register' ORDER BY id DESC LIMIT 1",
                    (email_norm,),
                )
                if latest and (now_ts - int(latest.get("created_at") or 0)) < 15:
                    raise ValueError("Please wait a few seconds before requesting a new code.")

                code = generate_6digit_code()
                code_hash = self._hash_code(email_norm, "register", code)
                self._execute(
                    conn,
                    "UPDATE verification_codes SET used_at = ? WHERE email = ? AND purpose = 'register' AND used_at = 0",
                    (now_ts, email_norm),
                )
                self._execute(
                    conn,
                    """
                    INSERT INTO verification_codes (email, purpose, code_hash, attempts, expires_at, used_at, created_at)
                    VALUES (?, 'register', ?, 0, ?, 0, ?)
                    """,
                    (email_norm, code_hash, now_ts + 900, now_ts),
                )
                conn.commit()
        return {"user": self._format_user(user_row), "code": code}

    def verify_customer_email(self, email: str, code: str) -> Dict[str, Any]:
        email_norm = self._norm_email(email)
        code_clean = str(code or "").strip()
        if not code_clean.isdigit() or len(code_clean) != 6:
            raise ValueError("Please enter the 6-digit verification code.")
        now_ts = int(time.time())

        with self._lock:
            with self._connect() as conn:
                user_row = self._fetch_one(conn, "SELECT * FROM users WHERE email = ?", (email_norm,))
                if not user_row:
                    raise ValueError("Account not found.")
                if user_row.get("status") != "active":
                    raise ValueError("This account is disabled. Contact support.")

                vc = self._fetch_one(
                    conn,
                    """
                    SELECT * FROM verification_codes
                    WHERE email = ? AND purpose = 'register' AND used_at = 0
                    ORDER BY id DESC LIMIT 1
                    """,
                    (email_norm,),
                )
                if not vc or now_ts > int(vc.get("expires_at") or 0):
                    raise ValueError("Verification code expired or not found. Please request a new code.")
                if int(vc.get("attempts") or 0) >= 5:
                    raise ValueError("Too many incorrect attempts. Please request a new verification code.")

                expected_hash = self._hash_code(email_norm, "register", code_clean)
                if not hmac.compare_digest(expected_hash, str(vc.get("code_hash") or "")):
                    self._execute(
                        conn,
                        "UPDATE verification_codes SET attempts = attempts + 1 WHERE id = ?",
                        (int(vc["id"]),),
                    )
                    conn.commit()
                    raise ValueError("Invalid verification code.")

                was_verified = int(user_row.get("email_verified") or 0) == 1
                self._execute(
                    conn,
                    "UPDATE verification_codes SET used_at = ? WHERE id = ?",
                    (now_ts, int(vc["id"])),
                )
                self._execute(
                    conn,
                    "UPDATE users SET email_verified = 1, last_login_at = ?, updated_at = ? WHERE id = ?",
                    (now_ts, now_ts, int(user_row["id"])),
                )
                self._link_customer_records_conn(conn, int(user_row["id"]), email_norm)
                conn.commit()
                updated = self._fetch_one(conn, "SELECT * FROM users WHERE id = ?", (int(user_row["id"]),))

        if not was_verified and updated:
            self.add_notification(
                event_type="NEW_CUSTOMER",
                severity="info",
                title=f"New Verified Customer: {updated['full_name']}",
                message=f"Customer account verified: {updated['full_name']} ({updated['email']})",
            )
        return self._format_user(updated) or {}

    def authenticate_customer(self, email: str, password: str) -> Dict[str, Any]:
        email_norm = self._norm_email(email)
        now_ts = int(time.time())
        with self._lock:
            with self._connect() as conn:
                user_row = self._fetch_one(conn, "SELECT * FROM users WHERE email = ?", (email_norm,))
                if not user_row or not verify_password(password, user_row.get("password_hash", "")):
                    raise ValueError("Invalid email or password.")
                if user_row.get("status") != "active":
                    raise ValueError("Your account has been disabled. Please contact support.")
                if int(user_row.get("email_verified") or 0) != 1:
                    raise PermissionError("EMAIL_UNVERIFIED")

                self._execute(
                    conn,
                    "UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?",
                    (now_ts, now_ts, int(user_row["id"])),
                )
                self._link_customer_records_conn(conn, int(user_row["id"]), email_norm)
                conn.commit()
                updated = self._fetch_one(conn, "SELECT * FROM users WHERE id = ?", (int(user_row["id"]),))
                return self._format_user(updated) or {}

    def request_password_reset(self, email: str) -> Optional[Dict[str, Any]]:
        email_norm = self._norm_email(email)
        now_ts = int(time.time())
        with self._lock:
            with self._connect() as conn:
                user_row = self._fetch_one(conn, "SELECT * FROM users WHERE email = ?", (email_norm,))
                if not user_row or user_row.get("status") != "active" or int(user_row.get("email_verified") or 0) != 1:
                    return None
                code = generate_6digit_code()
                code_hash = self._hash_code(email_norm, "reset", code)
                self._execute(
                    conn,
                    "UPDATE verification_codes SET used_at = ? WHERE email = ? AND purpose = 'reset' AND used_at = 0",
                    (now_ts, email_norm),
                )
                self._execute(
                    conn,
                    """
                    INSERT INTO verification_codes (email, purpose, code_hash, attempts, expires_at, used_at, created_at)
                    VALUES (?, 'reset', ?, 0, ?, 0, ?)
                    """,
                    (email_norm, code_hash, now_ts + 900, now_ts),
                )
                conn.commit()
        return {"user": self._format_user(user_row), "code": code}

    def reset_password_with_code(self, email: str, code: str, new_password: str) -> Dict[str, Any]:
        email_norm = self._norm_email(email)
        code_clean = str(code or "").strip()
        if not code_clean.isdigit() or len(code_clean) != 6:
            raise ValueError("Please enter the 6-digit reset code.")
        pwd_hash = hash_password(new_password)
        now_ts = int(time.time())

        with self._lock:
            with self._connect() as conn:
                user_row = self._fetch_one(conn, "SELECT * FROM users WHERE email = ?", (email_norm,))
                if not user_row or user_row.get("status") != "active":
                    raise ValueError("Invalid or expired reset code.")

                vc = self._fetch_one(
                    conn,
                    """
                    SELECT * FROM verification_codes
                    WHERE email = ? AND purpose = 'reset' AND used_at = 0
                    ORDER BY id DESC LIMIT 1
                    """,
                    (email_norm,),
                )
                if not vc or now_ts > int(vc.get("expires_at") or 0):
                    raise ValueError("Reset code expired. Please request a new code.")
                if int(vc.get("attempts") or 0) >= 5:
                    raise ValueError("Too many incorrect attempts. Please request a new reset code.")

                expected_hash = self._hash_code(email_norm, "reset", code_clean)
                if not hmac.compare_digest(expected_hash, str(vc.get("code_hash") or "")):
                    self._execute(
                        conn,
                        "UPDATE verification_codes SET attempts = attempts + 1 WHERE id = ?",
                        (int(vc["id"]),),
                    )
                    conn.commit()
                    raise ValueError("Invalid reset code.")

                self._execute(
                    conn,
                    "UPDATE verification_codes SET used_at = ? WHERE id = ?",
                    (now_ts, int(vc["id"])),
                )
                self._execute(
                    conn,
                    """
                    UPDATE users
                    SET password_hash = ?, token_version = token_version + 1,
                        last_login_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (pwd_hash, now_ts, now_ts, int(user_row["id"])),
                )
                conn.commit()
                updated = self._fetch_one(conn, "SELECT * FROM users WHERE id = ?", (int(user_row["id"]),))
                return self._format_user(updated) or {}

    def get_customer_by_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            with self._connect() as conn:
                row = self._fetch_one(conn, "SELECT * FROM users WHERE id = ?", (int(user_id),))
                return self._format_user(row)

    def update_customer_profile(
        self,
        user_id: int,
        full_name: str,
        current_password: str = "",
        new_password: str = "",
    ) -> Dict[str, Any]:
        name_clean = str(full_name or "").strip()
        if len(name_clean) < 2:
            raise ValueError("Full name must be at least 2 characters.")
        now_ts = int(time.time())
        with self._lock:
            with self._connect() as conn:
                user_row = self._fetch_one(conn, "SELECT * FROM users WHERE id = ?", (int(user_id),))
                if not user_row or user_row.get("status") != "active":
                    raise ValueError("Account not found or disabled.")
                if new_password:
                    if not verify_password(current_password, user_row.get("password_hash", "")):
                        raise ValueError("Current password is incorrect.")
                    new_hash = hash_password(new_password)
                    self._execute(
                        conn,
                        """
                        UPDATE users
                        SET full_name = ?, password_hash = ?, token_version = token_version + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (name_clean, new_hash, now_ts, int(user_id)),
                    )
                else:
                    self._execute(
                        conn,
                        "UPDATE users SET full_name = ?, updated_at = ? WHERE id = ?",
                        (name_clean, now_ts, int(user_id)),
                    )
                conn.commit()
                updated = self._fetch_one(conn, "SELECT * FROM users WHERE id = ?", (int(user_id),))
                return self._format_user(updated) or {}

    def get_customer_overview(self, user_id: int) -> Dict[str, Any]:
        with self._lock:
            with self._connect() as conn:
                user_row = self._fetch_one(conn, "SELECT * FROM users WHERE id = ?", (int(user_id),))
                if not user_row:
                    raise ValueError("Account not found.")
                email_norm = str(user_row["email"]).strip().lower()
                lic_rows = self._fetch_all(
                    conn,
                    "SELECT * FROM licenses WHERE user_id = ? OR LOWER(TRIM(customer_email)) = ? ORDER BY id DESC",
                    (int(user_id), email_norm),
                )
                ord_rows = self._fetch_all(
                    conn,
                    "SELECT * FROM orders WHERE user_id = ? OR LOWER(TRIM(customer_email)) = ? ORDER BY id DESC",
                    (int(user_id), email_norm),
                )
                req_rows = self._fetch_all(
                    conn,
                    "SELECT * FROM mt5_requests WHERE user_id = ? OR LOWER(TRIM(customer_email)) = ? ORDER BY id DESC",
                    (int(user_id), email_norm),
                )
                licenses = [self._format_license(r) for r in lic_rows]
                orders = [self._format_order(r, include_screenshot=False) for r in ord_rows]
                has_active = any(l["status"] == "active" and not l["is_expired"] for l in licenses)
                return {
                    "user": self._format_user(user_row),
                    "licenses": licenses,
                    "orders": orders,
                    "mt5_requests": req_rows,
                    "has_active_license": has_active,
                }

    def create_mt5_switch_request(
        self,
        user_id: int,
        license_key: str,
        new_mt5_login: str,
        reason: str = "",
    ) -> Dict[str, Any]:
        key = str(license_key or "").strip().upper()
        new_mt5 = str(new_mt5_login or "").strip()
        if not new_mt5.isdigit() or len(new_mt5) < 3:
            raise ValueError("Please enter a valid numeric MT5 Account Number.")
        reason_clean = str(reason or "").strip()[:400]
        now_ts = int(time.time())

        with self._lock:
            with self._connect() as conn:
                user_row = self._fetch_one(conn, "SELECT * FROM users WHERE id = ?", (int(user_id),))
                if not user_row or user_row.get("status") != "active":
                    raise ValueError("Account not found or disabled.")
                email_norm = str(user_row["email"]).strip().lower()

                lic_row = self._fetch_one(conn, "SELECT * FROM licenses WHERE license_key = ?", (key,))
                if not lic_row:
                    raise ValueError("License key not found.")
                if int(lic_row.get("user_id") or 0) != int(user_id) and str(lic_row.get("customer_email") or "").strip().lower() != email_norm:
                    raise ValueError("This license key does not belong to your account.")

                lic = self._format_license(lic_row)
                if lic["status"] != "active" or lic["is_expired"]:
                    raise ValueError("Only active, non-expired licenses can request an MT5 switch.")
                old_mt5 = str(lic["bound_mt5_login"]).strip()
                if new_mt5 == old_mt5:
                    raise ValueError("New MT5 account must be different from your current locked MT5 account.")

                existing_pending = self._fetch_one(
                    conn,
                    "SELECT id FROM mt5_requests WHERE license_key = ? AND status = 'pending'",
                    (key,),
                )
                if existing_pending:
                    raise ValueError("You already have a pending MT5 switch request for this license key.")

                self._execute(
                    conn,
                    """
                    INSERT INTO mt5_requests (
                        user_id, customer_email, customer_name, license_key,
                        old_mt5_login, new_mt5_login, reason, status, admin_note,
                        created_at, reviewed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '', ?, 0)
                    """,
                    (int(user_id), email_norm, user_row["full_name"], key, old_mt5, new_mt5, reason_clean, now_ts),
                )
                conn.commit()
                req_row = self._fetch_one(conn, "SELECT * FROM mt5_requests ORDER BY id DESC LIMIT 1")

        self.add_notification(
            event_type="MT5_SWITCH_REQUEST",
            severity="gold",
            title=f"MT5 Switch Request: {key}",
            message=(
                f"{user_row['full_name']} ({email_norm}) requests moving {key} "
                f"from MT5 #{old_mt5} to #{new_mt5}. Reason: {reason_clean or 'Not specified'}"
            ),
            license_key=key,
        )
        return req_row or {}

    def get_mt5_requests(self, status_filter: str = "all") -> List[Dict[str, Any]]:
        with self._lock:
            with self._connect() as conn:
                if status_filter and status_filter != "all":
                    return self._fetch_all(
                        conn,
                        "SELECT * FROM mt5_requests WHERE status = ? ORDER BY id DESC LIMIT 200",
                        (status_filter,),
                    )
                return self._fetch_all(conn, "SELECT * FROM mt5_requests ORDER BY id DESC LIMIT 200")

    def approve_mt5_switch_request(self, req_id: int, admin_note: str = "") -> Dict[str, Any]:
        now_ts = int(time.time())
        with self._lock:
            with self._connect() as conn:
                req_row = self._fetch_one(conn, "SELECT * FROM mt5_requests WHERE id = ?", (int(req_id),))
                if not req_row:
                    raise ValueError("MT5 switch request not found.")
                if req_row["status"] != "pending":
                    raise ValueError(f"Request #{req_id} was already {req_row['status']}.")

                key = req_row["license_key"]
                lic_row = self._fetch_one(conn, "SELECT * FROM licenses WHERE license_key = ?", (key,))
                if not lic_row:
                    raise ValueError(f"License {key} no longer exists.")

                new_mt5 = str(req_row["new_mt5_login"]).strip()
                self._execute(
                    conn,
                    "UPDATE licenses SET bound_mt5_login = ?, updated_at = ? WHERE license_key = ?",
                    (new_mt5, now_ts, key),
                )
                self._execute(
                    conn,
                    "UPDATE mt5_requests SET status = 'approved', admin_note = ?, reviewed_at = ? WHERE id = ?",
                    (admin_note or "Approved by Admin", now_ts, int(req_id)),
                )
                conn.commit()
                updated_req = self._fetch_one(conn, "SELECT * FROM mt5_requests WHERE id = ?", (int(req_id),))
                updated_lic = self._fetch_one(conn, "SELECT * FROM licenses WHERE license_key = ?", (key,))

        self.add_notification(
            event_type="MT5_SWITCH_APPROVED",
            severity="success",
            title=f"MT5 Switch Approved: {key}",
            message=f"{req_row['customer_name']} ({req_row['customer_email']}) moved {key} from #{req_row['old_mt5_login']} to #{req_row['new_mt5_login']}.",
            license_key=key,
        )
        return {"request": updated_req, "license": self._format_license(updated_lic)}

    def reject_mt5_switch_request(self, req_id: int, reason: str = "") -> Dict[str, Any]:
        now_ts = int(time.time())
        with self._lock:
            with self._connect() as conn:
                req_row = self._fetch_one(conn, "SELECT * FROM mt5_requests WHERE id = ?", (int(req_id),))
                if not req_row:
                    raise ValueError("MT5 switch request not found.")
                if req_row["status"] != "pending":
                    raise ValueError(f"Request #{req_id} was already {req_row['status']}.")

                note = reason or "Switch request declined by Admin"
                self._execute(
                    conn,
                    "UPDATE mt5_requests SET status = 'rejected', admin_note = ?, reviewed_at = ? WHERE id = ?",
                    (note, now_ts, int(req_id)),
                )
                conn.commit()
                updated_req = self._fetch_one(conn, "SELECT * FROM mt5_requests WHERE id = ?", (int(req_id),))

        self.add_notification(
            event_type="MT5_SWITCH_REJECTED",
            severity="warn",
            title=f"MT5 Switch Rejected: {req_row['license_key']}",
            message=f"{req_row['customer_name']} ({req_row['customer_email']}) declined: {note}",
            license_key=req_row["license_key"],
        )
        return updated_req or {}

    def get_users(self, search: str = "") -> List[Dict[str, Any]]:
        with self._lock:
            with self._connect() as conn:
                users = self._fetch_all(conn, "SELECT * FROM users ORDER BY id DESC LIMIT 300")
                licenses = self._fetch_all(conn, "SELECT user_id, customer_email, status, expires_at FROM licenses")
                orders = self._fetch_all(conn, "SELECT user_id, customer_email, status, amount_eur FROM orders")
                now_ts = int(time.time())

                out: List[Dict[str, Any]] = []
                q = search.strip().lower()
                for u in users:
                    fmt = self._format_user(u)
                    uid = int(u["id"])
                    email_norm = str(u["email"]).strip().lower()
                    u_lics = [
                        l for l in licenses
                        if int(l.get("user_id") or 0) == uid or str(l.get("customer_email") or "").strip().lower() == email_norm
                    ]
                    u_ords = [
                        o for o in orders
                        if int(o.get("user_id") or 0) == uid or str(o.get("customer_email") or "").strip().lower() == email_norm
                    ]
                    fmt["license_count"] = len(u_lics)
                    fmt["active_license_count"] = sum(
                        1 for l in u_lics
                        if l["status"] == "active" and (int(l.get("expires_at") or 0) == 0 or int(l["expires_at"]) > now_ts)
                    )
                    fmt["order_count"] = len(u_ords)
                    fmt["total_spent_eur"] = round(
                        sum(float(o.get("amount_eur") or 0) for o in u_ords if o["status"] == "approved"),
                        2,
                    )
                    if not q or q in fmt["full_name"].lower() or q in fmt["email"].lower():
                        out.append(fmt)
                return out

    def update_user_status(self, user_id: int, status: str) -> Dict[str, Any]:
        if status not in ("active", "disabled"):
            raise ValueError("Status must be 'active' or 'disabled'.")
        now_ts = int(time.time())
        with self._lock:
            with self._connect() as conn:
                user_row = self._fetch_one(conn, "SELECT * FROM users WHERE id = ?", (int(user_id),))
                if not user_row:
                    raise ValueError("User not found.")
                self._execute(
                    conn,
                    "UPDATE users SET status = ?, token_version = token_version + 1, updated_at = ? WHERE id = ?",
                    (status, now_ts, int(user_id)),
                )
                conn.commit()
                updated = self._fetch_one(conn, "SELECT * FROM users WHERE id = ?", (int(user_id),))
                return self._format_user(updated) or {}

    # ================= ORDERS & LICENSES =================

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
        user_id: int = 0,
    ) -> Dict[str, Any]:
        clean_bots = [b for b in dict.fromkeys(bot_ids) if b in BOT_BY_ID]
        if not clean_bots:
            raise ValueError("Please select at least one Expert Advisor.")
        mt5_clean = str(mt5_login or "").strip()
        if not mt5_clean.isdigit() or len(mt5_clean) < 3:
            raise ValueError("Please enter a valid numeric MT5 Account Number.")
        email_clean = self._norm_email(customer_email)
        name_clean = str(customer_name or "").strip()
        if len(name_clean) < 2:
            raise ValueError("Please enter your full name.")

        plan = "monthly" if str(plan_type).lower() in ("monthly", "30_days", "30days") else "lifetime"
        amount = float(calculate_order_price_eur(plan, clean_bots, bundle_id=bundle_id))
        now_ts = int(time.time())

        with self._lock:
            with self._connect() as conn:
                resolved_uid = int(user_id or 0)
                if resolved_uid == 0:
                    u = self._fetch_one(conn, "SELECT id FROM users WHERE email = ?", (email_clean,))
                    if u:
                        resolved_uid = int(u["id"])

                ref = generate_order_ref()
                while self._fetch_one(conn, "SELECT id FROM orders WHERE order_ref = ?", (ref,)):
                    ref = generate_order_ref()

                self._execute(
                    conn,
                    """
                    INSERT INTO orders (
                        order_ref, user_id, customer_name, customer_email, mt5_login, plan_type,
                        bundle_id, bot_ids_json, amount_eur, paypal_tx, screenshot_data,
                        status, created_at, reviewed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, 0)
                    """,
                    (
                        ref,
                        resolved_uid,
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

                resolved_uid = int(order.get("user_id") or 0)
                if resolved_uid == 0:
                    email_norm = str(order.get("customer_email") or "").strip().lower()
                    u = self._fetch_one(conn, "SELECT id FROM users WHERE email = ?", (email_norm,))
                    if u:
                        resolved_uid = int(u["id"])

                plan = order["plan_type"]
                expires_at = (now_ts + PLAN_MONTHLY_DAYS * 86400) if plan == "monthly" else 0
                self._execute(
                    conn,
                    """
                    INSERT INTO licenses (
                        license_key, user_id, customer_name, customer_email, bound_mt5_login,
                        plan_type, bot_ids_json, amount_eur, status, expires_at,
                        order_ref, notes, last_seen_at, last_seen_ip, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, 0, '', ?, ?)
                    """,
                    (
                        key,
                        resolved_uid,
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
        email_norm = str(customer_email or "").strip().lower()
        plan = "monthly" if str(plan_type).lower() in ("monthly", "30_days", "30days") else "lifetime"
        now_ts = int(time.time())
        expires_at = (now_ts + max(1, int(days)) * 86400) if plan == "monthly" else 0

        with self._lock:
            with self._connect() as conn:
                resolved_uid = 0
                if email_norm:
                    u = self._fetch_one(conn, "SELECT id FROM users WHERE email = ?", (email_norm,))
                    if u:
                        resolved_uid = int(u["id"])
                key = generate_unique_license_key()
                while self._fetch_one(conn, "SELECT id FROM licenses WHERE license_key = ?", (key,)):
                    key = generate_unique_license_key()
                self._execute(
                    conn,
                    """
                    INSERT INTO licenses (
                        license_key, user_id, customer_name, customer_email, bound_mt5_login,
                        plan_type, bot_ids_json, amount_eur, status, expires_at,
                        order_ref, notes, last_seen_at, last_seen_ip, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, 'MANUAL', ?, 0, '', ?, ?)
                    """,
                    (
                        key,
                        resolved_uid,
                        customer_name.strip() or "Customer",
                        email_norm or customer_email.strip(),
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
                users_row = self._fetch_one(conn, "SELECT COUNT(*) AS cnt FROM users WHERE email_verified = 1")
                mt5_row = self._fetch_one(conn, "SELECT COUNT(*) AS cnt FROM mt5_requests WHERE status = 'pending'")
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
                    "total_users": int((users_row or {}).get("cnt", 0)),
                    "pending_mt5_requests": int((mt5_row or {}).get("cnt", 0)),
                }

    def rotate_growth_sync_token(self) -> str:
        new_tok = f"ADG-{secrets.token_urlsafe(24)}"
        self.update_settings({"growth_sync_token": new_tok})
        return new_tok

    def save_published_growth(
        self,
        snapshot: Dict[str, Any],
        mt5_login: str = "",
        currency: str = "USD",
        server_day: str = "",
        sync_reason: str = "daily_24h",
    ) -> Dict[str, Any]:
        if not isinstance(snapshot, dict) or "portfolio" not in snapshot or "bots" not in snapshot:
            raise ValueError("Invalid Growth payload: missing 'portfolio' or 'bots'.")
        now_ts = int(time.time())
        port = snapshot.get("portfolio") or {}
        inf = port.get("info") or {}
        meta = {
            "mt5_login": str(mt5_login or "").strip(),
            "currency": str(currency or "USD").strip() or "USD",
            "server_day": str(server_day or "").strip(),
            "sync_reason": str(sync_reason or "daily_24h").strip(),
            "synced_at": now_ts,
            "balance": float(inf.get("balance") or 0.0),
            "equity": float(inf.get("equity") or 0.0),
            "gain_pct": float(inf.get("gain_pct") or 0.0),
            "profit": float(inf.get("profit") or 0.0),
        }
        snap_json = json.dumps(snapshot, ensure_ascii=False)
        meta_json = json.dumps(meta, ensure_ascii=False)
        with self._lock:
            with self._connect() as conn:
                existing = self._fetch_one(conn, "SELECT id FROM growth_publish WHERE id = 1")
                if existing:
                    self._execute(
                        conn,
                        "UPDATE growth_publish SET snapshot_json = ?, meta_json = ?, updated_at = ? WHERE id = 1",
                        (snap_json, meta_json, now_ts),
                    )
                else:
                    self._execute(
                        conn,
                        "INSERT INTO growth_publish (id, snapshot_json, meta_json, updated_at) VALUES (1, ?, ?, ?)",
                        (snap_json, meta_json, now_ts),
                    )
                conn.commit()
        acct_txt = f"MT5 #{meta['mt5_login']}" if meta["mt5_login"] else "Connected Account"
        self.add_notification(
            event_type="GROWTH_SYNC",
            severity="success",
            title=f"Live Growth Synced ({sync_reason})",
            message=f"{acct_txt} | Day {meta['server_day'] or 'Today'} | Balance: ${meta['balance']:,.2f} | Gain: {meta['gain_pct']:+.2f}%",
        )
        return meta

    def get_published_growth(self) -> Dict[str, Any]:
        with self._lock:
            with self._connect() as conn:
                row = self._fetch_one(conn, "SELECT snapshot_json, meta_json, updated_at FROM growth_publish WHERE id = 1")
                if not row:
                    return {"has_data": False, "growth": None, "meta": {}}
                try:
                    snap = json.loads(row.get("snapshot_json") or "{}")
                except Exception:
                    snap = None
                try:
                    meta = json.loads(row.get("meta_json") or "{}")
                except Exception:
                    meta = {}
                return {
                    "has_data": bool(snap and isinstance(snap, dict) and "portfolio" in snap),
                    "growth": snap,
                    "meta": meta,
                }
