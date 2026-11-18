import http.cookiejar
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

SERVER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

from config import BOT_ORDER, calculate_order_price_eur
from main import create_app_server
from mailer import Mailer


class MockMailer(Mailer):
    def __init__(self, env_cfg, db=None):
        super().__init__(env_cfg, db=db)
        self.sent = []

    def is_configured(self) -> bool:
        return True

    def send_email(self, to_email: str, subject: str, text_body: str, html_body: str, order_ref: str = "", license_key: str = ""):
        self.sent.append({
            "to": to_email,
            "subject": subject,
            "text": text_body,
            "html": html_body,
            "order_ref": order_ref,
            "license_key": license_key,
        })
        if self.db is not None:
            self.db.log_email(
                recipient=to_email,
                subject=subject,
                status="SENT",
                provider="mock_smtp",
                order_ref=order_ref,
                license_key=license_key,
            )
        return {"ok": True, "provider": "mock_smtp", "error": ""}


class TestAurumDeskServer(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="aurum_srv_test_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.env = {
            "PORT": "0",
            "DATABASE_URL": "",
            "SQLITE_PATH": self.db_path,
            "ADMIN_PASSWORD": "SecretTestPassword!",
            "SECRET_KEY": "test-hmac-secret-key-aurum",
            "PAYPAL_ME_URL": "https://paypal.me/AurumDesk",
            "PUBLIC_CONTACT_EMAIL": "eliodemaj07@gmail.com",
            "DEFAULT_DOWNLOAD_URL": "https://example.com/Aurum_Desk_Windows.zip",
        }
        self.mock_mailer = MockMailer(self.env)
        self.server = create_app_server(port=0, env_override=self.env, mailer_override=self.mock_mailer)
        self.mock_mailer.db = self.server.db
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        self.admin_jar = http.cookiejar.CookieJar()
        self.admin_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.admin_jar))

        self.cust_jar = http.cookiejar.CookieJar()
        self.cust_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cust_jar))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _req(self, path, method="GET", body=None, role="anon", opener=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"} if body is not None else {}
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        if opener is None:
            if role == "admin":
                opener = self.admin_opener
            elif role == "customer":
                opener = self.cust_opener
            else:
                opener = urllib.request.build_opener()
        try:
            with opener.open(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def _extract_last_code(self, email: str) -> str:
        msgs = [m for m in self.mock_mailer.sent if m["to"].lower() == email.lower() and "Code:" in m["text"]]
        self.assertTrue(msgs, f"Expected verification/reset email for {email}")
        m = re.search(r"Verification Code:\s*(\d{6})", msgs[-1]["text"])
        self.assertIsNotNone(m, f"6-digit code missing in email: {msgs[-1]['text']}")
        return m.group(1)

    def _register_and_verify(self, name: str, email: str, password: str, opener=None):
        if opener is None:
            opener = self.cust_opener
        code, reg = self._req(
            "/api/auth/register",
            method="POST",
            body={"full_name": name, "email": email, "password": password},
            opener=opener,
        )
        self.assertEqual(code, 201)
        self.assertTrue(reg["verification_required"])
        otp = self._extract_last_code(email)
        code, ver = self._req(
            "/api/auth/verify-email",
            method="POST",
            body={"email": email, "code": otp},
            opener=opener,
        )
        self.assertEqual(code, 200)
        self.assertTrue(ver["ok"])
        return ver["user"]

    def test_01_pricing_and_bundles_exact(self):
        self.assertEqual(calculate_order_price_eur("lifetime", ["zenith"]), 60)
        self.assertEqual(calculate_order_price_eur("lifetime", ["twoshots"]), 30)
        self.assertEqual(calculate_order_price_eur("lifetime", ["mox"]), 50)
        self.assertEqual(calculate_order_price_eur("lifetime", ["momentum"]), 20)
        self.assertEqual(calculate_order_price_eur("lifetime", ["agnesa"]), 30)
        self.assertEqual(calculate_order_price_eur("lifetime", ["multiversus"]), 90)
        self.assertEqual(calculate_order_price_eur("lifetime", ["albaniang"]), 50)
        for bid in BOT_ORDER:
            self.assertEqual(calculate_order_price_eur("monthly", [bid]), 15)

        self.assertEqual(calculate_order_price_eur("lifetime", BOT_ORDER, bundle_id="full_7"), 249)
        self.assertEqual(calculate_order_price_eur("monthly", BOT_ORDER, bundle_id="full_7"), 75)
        self.assertEqual(calculate_order_price_eur("lifetime", ["twoshots", "agnesa", "momentum"]), 65)

    def test_02_public_pages_catalog_and_admin_auth(self):
        for route in ("/", "/account"):
            with urllib.request.urlopen(self.base + route) as r:
                store_html = r.read().decode("utf-8")
                self.assertIn("paypal.me/AurumDesk", store_html)
                self.assertIn("Aurum Desk", store_html)
                self.assertIn("My Account", store_html)

        with urllib.request.urlopen(self.base + "/admin") as r:
            admin_html = r.read().decode("utf-8")
            self.assertIn("Live Notifications", admin_html)
            self.assertIn("MT5 Switch Requests", admin_html)
            self.assertIn("Customers", admin_html)

        code, health = self._req("/health")
        self.assertEqual(code, 200)
        self.assertTrue(health["ok"])

        for head_path in ("/", "/account", "/health", "/admin"):
            hreq = urllib.request.Request(self.base + head_path, method="HEAD")
            with urllib.request.urlopen(hreq) as hr:
                self.assertEqual(hr.status, 200)

        code, _ = self._req("/admin/api/overview", role="admin")
        self.assertEqual(code, 401)

        code, _ = self._req("/admin/api/login", method="POST", body={"password": "wrong"}, role="admin")
        self.assertEqual(code, 401)

        code, ok_login = self._req("/admin/api/login", method="POST", body={"password": "SecretTestPassword!"}, role="admin")
        self.assertEqual(code, 200)
        self.assertTrue(ok_login["ok"])

        code, ov = self._req("/admin/api/overview", role="admin")
        self.assertEqual(code, 200)
        self.assertTrue(ov["ok"])

    def test_03_required_account_checkout_approval_download_gate_and_mt5_lock(self):
        # 0) Unauthenticated customer cannot submit PayPal order or access /api/account/overview
        code, blocked_ord = self._req("/api/orders", method="POST", body={
            "customer_name": "Guest",
            "customer_email": "guest@example.com",
            "mt5_login": "55511122",
            "plan_type": "lifetime",
            "bot_ids": ["zenith"],
        })
        self.assertEqual(code, 401)
        self.assertTrue(blocked_ord["auth_required"])

        code, _ = self._req("/api/account/overview")
        self.assertEqual(code, 401)

        # 1) Login as admin
        self._req("/admin/api/login", method="POST", body={"password": "SecretTestPassword!"}, role="admin")

        # 2) Customer creates account & verifies 6-digit email code
        user = self._register_and_verify("Alex Trader", "alex@example.com", "StrongPass123!")
        self.assertEqual(user["email"], "alex@example.com")

        # Before any active license, download_url must remain hidden
        code, acct0 = self._req("/api/account/overview", role="customer")
        self.assertEqual(code, 200)
        self.assertFalse(acct0["has_active_license"])
        self.assertEqual(acct0["download_url"], "")

        # 3) Authenticated customer submits PayPal order for Zenith + Albanian G (€110 Lifetime) on MT5 #55511122
        code, created = self._req("/api/orders", method="POST", body={
            "customer_name": "Alex Trader",
            "mt5_login": "55511122",
            "plan_type": "lifetime",
            "bot_ids": ["zenith", "albaniang"],
            "paypal_tx": "TXN-998877",
            "screenshot_data": "data:image/jpeg;base64,/9j/4AAQSkZJRg==",
        }, role="customer")
        self.assertEqual(code, 201)
        order = created["order"]
        self.assertEqual(order["amount_eur"], 110.0)
        self.assertEqual(order["status"], "pending")
        self.assertEqual(order["customer_email"], "alex@example.com")
        ref = order["order_ref"]

        # 4) Check Live Notifications arrived in Admin Panel (NEW_CUSTOMER + NEW_ORDER)
        _, ov = self._req("/admin/api/overview", role="admin")
        self.assertEqual(ov["notifications"][0]["event_type"], "NEW_ORDER")
        self.assertEqual(ov["metrics"]["pending_orders"], 1)
        self.assertEqual(ov["metrics"]["total_users"], 1)

        # 5) Admin approves order -> generates Unique Key & emails customer
        code, app_res = self._req(f"/admin/api/orders/{ref}/approve", method="POST", body={}, role="admin")
        self.assertEqual(code, 200)
        self.assertTrue(app_res["ok"])
        key = app_res["license"]["license_key"]
        self.assertTrue(key.startswith("AURUM-"))
        self.assertEqual(app_res["license"]["bound_mt5_login"], "55511122")
        self.assertEqual(app_res["license"]["bot_ids"], ["zenith", "albaniang"])

        # Download link now unlocks inside customer's My Account!
        code, acct1 = self._req("/api/account/overview", role="customer")
        self.assertEqual(code, 200)
        self.assertTrue(acct1["has_active_license"])
        self.assertEqual(acct1["download_url"], "https://example.com/Aurum_Desk_Windows.zip")
        self.assertEqual(len(acct1["licenses"]), 1)
        self.assertEqual(acct1["licenses"][0]["license_key"], key)

        cust_key_emails = [m for m in self.mock_mailer.sent if m["to"] == "alex@example.com" and key in m["text"]]
        self.assertEqual(len(cust_key_emails), 1)
        self.assertIn("#55511122", cust_key_emails[0]["text"])
        self.assertIn("https://example.com/Aurum_Desk_Windows.zip", cust_key_emails[0]["text"])

        # 6) Public order tracker confirms approved without leaking full key
        code, trk = self._req(f"/api/order-status/{ref}")
        self.assertEqual(code, 200)
        self.assertEqual(trk["order"]["status"], "approved")
        self.assertNotIn("license_key", trk["order"])

        # 7) Desktop app verifies key BEFORE & WITH matching MT5 #55511122
        code, v0 = self._req("/api/license/verify", method="POST", body={"license_key": key, "mt5_login": ""})
        self.assertEqual(code, 200)
        self.assertFalse(v0["mt5_verified"])

        code, v1 = self._req("/api/license/verify", method="POST", body={"license_key": key, "mt5_login": "55511122"})
        self.assertEqual(code, 200)
        self.assertTrue(v1["mt5_verified"])
        self.assertEqual(v1["allowed_bots"], ["zenith", "albaniang"])

        # 8) Another MT5 account (#99988877) blocked + Danger alert
        code, v_bad = self._req("/api/license/verify", method="POST", body={"license_key": key, "mt5_login": "99988877"})
        self.assertEqual(code, 403)
        self.assertEqual(v_bad["error"], "account_mismatch")

        _, ov2 = self._req("/admin/api/overview", role="admin")
        self.assertEqual(ov2["notifications"][0]["event_type"], "MT5_MISMATCH")

        # 9) Customer submits MT5 Switch Request -> Admin approves -> license rebound & emailed
        code, sw_res = self._req("/api/account/mt5-switch", method="POST", body={
            "license_key": key,
            "new_mt5_login": "77766655",
            "reason": "Switched to live funded account",
        }, role="customer")
        self.assertEqual(code, 201)
        sw_id = sw_res["request"]["id"]

        # Duplicate pending request blocked
        code, sw_dup = self._req("/api/account/mt5-switch", method="POST", body={
            "license_key": key,
            "new_mt5_login": "88899900",
        }, role="customer")
        self.assertEqual(code, 400)

        code, sw_app = self._req(f"/admin/api/mt5-requests/{sw_id}/approve", method="POST", body={}, role="admin")
        self.assertEqual(code, 200)
        self.assertEqual(sw_app["license"]["bound_mt5_login"], "77766655")

        code, v_old = self._req("/api/license/verify", method="POST", body={"license_key": key, "mt5_login": "55511122"})
        self.assertEqual(code, 403)
        code, v_new = self._req("/api/license/verify", method="POST", body={"license_key": key, "mt5_login": "77766655"})
        self.assertEqual(code, 200)
        self.assertTrue(v_new["mt5_verified"])

        # 10) Admin suspends license -> verify fails & download link locks again
        self._req(f"/admin/api/licenses/{key}", method="POST", body={"status": "suspended"}, role="admin")
        code, v_susp = self._req("/api/license/verify", method="POST", body={"license_key": key, "mt5_login": "77766655"})
        self.assertEqual(code, 400)
        self.assertEqual(v_susp["error"], "license_suspended")

        code, acct2 = self._req("/api/account/overview", role="customer")
        self.assertFalse(acct2["has_active_license"])
        self.assertEqual(acct2["download_url"], "")

    def test_04_password_reset_legacy_linking_and_user_disable(self):
        # 1) Create a manual license BEFORE the customer registers (simulating existing Neon licenses)
        self._req("/admin/api/login", method="POST", body={"password": "SecretTestPassword!"}, role="admin")
        code, man = self._req("/admin/api/licenses", method="POST", body={
            "customer_name": "Legacy Trader",
            "customer_email": "legacy@example.com",
            "mt5_login": "44433322",
            "plan_type": "lifetime",
            "bot_ids": ["mox"],
            "amount_eur": 50,
        }, role="admin")
        self.assertEqual(code, 201)
        legacy_key = man["license"]["license_key"]

        # 2) Register unverified -> attempting login before verifying triggers 403 verification_required
        code, _ = self._req("/api/auth/register", method="POST", body={
            "full_name": "Legacy Trader",
            "email": "Legacy@Example.com",
            "password": "OldPassword123!",
        }, opener=self.cust_opener)
        self.assertEqual(code, 201)

        code, unv_login = self._req("/api/auth/login", method="POST", body={
            "email": "legacy@example.com",
            "password": "OldPassword123!",
        }, opener=self.cust_opener)
        self.assertEqual(code, 403)
        self.assertTrue(unv_login["verification_required"])

        # Wrong verification code fails
        code, bad_ver = self._req("/api/auth/verify-email", method="POST", body={
            "email": "legacy@example.com",
            "code": "000000",
        }, opener=self.cust_opener)
        self.assertEqual(code, 400)

        # Correct verification code verifies AND auto-links the pre-existing manual license!
        otp = self._extract_last_code("legacy@example.com")
        code, ok_ver = self._req("/api/auth/verify-email", method="POST", body={
            "email": "legacy@example.com",
            "code": otp,
        }, opener=self.cust_opener)
        self.assertEqual(code, 200)
        uid = ok_ver["user"]["id"]

        code, ov = self._req("/api/account/overview", role="customer")
        self.assertEqual(code, 200)
        self.assertTrue(ov["has_active_license"])
        self.assertEqual(ov["licenses"][0]["license_key"], legacy_key)

        # 3) Keep an old cookie jar, reset password in a second jar -> old cookie jar MUST be invalidated!
        jar2 = http.cookiejar.CookieJar()
        opener2 = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar2))

        code, fp = self._req("/api/auth/forgot-password", method="POST", body={"email": "legacy@example.com"}, opener=opener2)
        self.assertEqual(code, 200)
        reset_otp = self._extract_last_code("legacy@example.com")

        code, rp = self._req("/api/auth/reset-password", method="POST", body={
            "email": "legacy@example.com",
            "code": reset_otp,
            "new_password": "NewStrongPassword999!",
        }, opener=opener2)
        self.assertEqual(code, 200)

        # Old session cookie is now rejected
        code, old_sess = self._req("/api/account/overview", role="customer")
        self.assertEqual(code, 401)
        # New session cookie works
        code, new_sess = self._req("/api/account/overview", opener=opener2)
        self.assertEqual(code, 200)

        # 4) Admin disables customer -> opener2 session is immediately invalidated
        code, dis = self._req(f"/admin/api/users/{uid}/status", method="POST", body={"status": "disabled"}, role="admin")
        self.assertEqual(code, 200)
        self.assertEqual(dis["user"]["status"], "disabled")

        code, kicked = self._req("/api/account/overview", opener=opener2)
        self.assertEqual(code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
