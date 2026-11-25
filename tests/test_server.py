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
            "PAYPAL_ME_URL": "https://paypal.me/AurmDesk",
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
        # Random 3-EA & 4-EA bundle picks maintain bundle discount & never exceed individual sum
        self.assertEqual(calculate_order_price_eur("lifetime", ["zenith", "multiversus", "mox"], bundle_id="starter_3"), 65)
        self.assertEqual(calculate_order_price_eur("monthly", ["zenith", "multiversus", "mox"], bundle_id="starter_3"), 35)
        self.assertEqual(calculate_order_price_eur("lifetime", ["zenith", "mox", "multiversus", "albaniang"], bundle_id="pro_4"), 210)
        self.assertEqual(calculate_order_price_eur("lifetime", ["momentum", "twoshots", "agnesa", "mox"], bundle_id="pro_4"), 109)
        self.assertEqual(calculate_order_price_eur("monthly", ["momentum", "twoshots", "agnesa", "mox"], bundle_id="pro_4"), 49)

    def test_02_public_pages_catalog_and_admin_auth(self):
        for route in ("/", "/account"):
            with urllib.request.urlopen(self.base + route) as r:
                store_html = r.read().decode("utf-8")
                self.assertIn("paypal.me/AurmDesk", store_html)
                self.assertIn("Aurum Desk", store_html)
                self.assertIn("My Account", store_html)

        code, cat = self._req("/api/catalog")
        self.assertEqual(code, 200)
        self.assertEqual(cat["paypal_me_url"], "https://paypal.me/AurmDesk")
        bots_by_id = {b["id"]: b for b in cat["bots"]}
        self.assertEqual(bots_by_id["twoshots"]["name"], "GOLDEN PULSE EA")
        self.assertEqual(bots_by_id["twoshots"]["timeframe"], "H4 Box / M5")
        self.assertEqual(bots_by_id["momentum"]["timeframe"], "M5")
        self.assertEqual(bots_by_id["agnesa"]["timeframe"], "M5 / H1")

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
        self.assertEqual(ov["effective_paypal_me_url"], "https://paypal.me/AurmDesk")

        # Admin can change PayPal.me URL directly in Settings and /api/catalog reflects it immediately
        code, set_res = self._req("/admin/api/settings", method="POST", body={"paypal_me_url": "paypal.me/CustomTestPay"}, role="admin")
        self.assertEqual(code, 200)
        self.assertEqual(set_res["effective_paypal_me_url"], "https://paypal.me/CustomTestPay")
        code, cat2 = self._req("/api/catalog")
        self.assertEqual(cat2["paypal_me_url"], "https://paypal.me/CustomTestPay")
        # Reset back to env fallback
        self._req("/admin/api/settings", method="POST", body={"paypal_me_url": ""}, role="admin")
        code, cat3 = self._req("/api/catalog")
        self.assertEqual(cat3["paypal_me_url"], "https://paypal.me/AurmDesk")

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

    def test_05_download_url_auto_heal_random_bundles_and_live_visitors(self):
        from config import OFFICIAL_DOWNLOAD_URL, resolve_download_url
        from db import Database

        # 1) Placeholder download_url resolves & auto-heals existing DB setting to official Google Drive URL
        self.assertEqual(resolve_download_url("https://aurumdesk.com/download-coming-soon"), OFFICIAL_DOWNLOAD_URL)
        self.assertEqual(resolve_download_url(""), OFFICIAL_DOWNLOAD_URL)

        tmp_heal = os.path.join(self.tmpdir, "heal_test.db")
        db_heal = Database(database_url="", sqlite_path=tmp_heal, default_download_url="https://aurumdesk.com/download-coming-soon", secret_key="k")
        self.assertEqual(db_heal.get_settings()["download_url"], OFFICIAL_DOWNLOAD_URL)
        db_heal.update_settings({"download_url": "https://aurumdesk.com/download-coming-soon"})
        db_heal2 = Database(database_url="", sqlite_path=tmp_heal, default_download_url="https://aurumdesk.com/download-coming-soon", secret_key="k")
        self.assertEqual(db_heal2.get_settings()["download_url"], OFFICIAL_DOWNLOAD_URL)

        # 2) Catalog bundles advertise random_pick flags for Starter 3 & Pro 4
        code, cat = self._req("/api/catalog")
        self.assertEqual(code, 200)
        bundles_by_id = {b["id"]: b for b in cat["bundles"]}
        self.assertTrue(bundles_by_id["starter_3"]["random_pick"])
        self.assertEqual(bundles_by_id["starter_3"]["pack_count"], 3)
        self.assertTrue(bundles_by_id["pro_4"]["random_pick"])
        self.assertEqual(bundles_by_id["pro_4"]["pack_count"], 4)
        self.assertFalse(bundles_by_id["full_7"]["random_pick"])

        # 3) Live Website Visitors endpoint requires admin auth
        code, _ = self._req("/admin/api/live-visitors", role="admin")
        self.assertEqual(code, 401)

        self._req("/admin/api/login", method="POST", body={"password": "SecretTestPassword!"}, role="admin")

        # 4) Guest pings Store + Verified Customer pings Account -> Live counter shows 2 people!
        code, p1 = self._req("/api/presence/ping", method="POST", body={"visitor_id": "v_guest_1", "tab_id": "t_g1", "page": "store"})
        self.assertEqual(code, 200)
        self.assertEqual(p1["online_people"], 1)

        self._register_and_verify("Live Viewer", "viewer@example.com", "ViewerPass123!")
        code, p2 = self._req("/api/presence/ping", method="POST", body={"visitor_id": "v_cust_2", "tab_id": "t_c2", "page": "account"}, role="customer")
        self.assertEqual(code, 200)
        self.assertEqual(p2["online_people"], 2)

        code, lv_res = self._req("/admin/api/live-visitors", role="admin")
        self.assertEqual(code, 200)
        lv = lv_res["live_visitors"]
        self.assertEqual(lv["online_people"], 2)
        self.assertEqual(lv["on_store"], 1)
        self.assertEqual(lv["on_account"], 1)
        self.assertEqual(lv["logged_in"], 1)
        self.assertEqual(lv["guests"], 1)
        self.assertGreaterEqual(lv["peak_today"], 2)
        labels = [x["label"] for x in lv["visitors"]]
        self.assertIn("Live Viewer", labels)

        # 5) Leaving tab immediately drops online_people down to 1
        code, _ = self._req("/api/presence/leave", method="POST", body={"visitor_id": "v_guest_1", "tab_id": "t_g1"})
        self.assertEqual(code, 200)
        code, ov = self._req("/admin/api/overview", role="admin")
        self.assertEqual(code, 200)
        self.assertEqual(ov["live_visitors"]["online_people"], 1)
        self.assertEqual(ov["live_visitors"]["guests"], 0)
        self.assertEqual(ov["live_visitors"]["logged_in"], 1)

    def test_06_live_growth_sync_and_public_view(self):
        # 1) Public /growth page serves HTML containing the Growth window matching Windows app
        with urllib.request.urlopen(self.base + "/growth") as resp:
            self.assertEqual(resp.status, 200)
            html_body = resp.read().decode("utf-8")
        self.assertIn('id="growthView"', html_body)
        self.assertIn('id="growthChartCanvas"', html_body)
        self.assertIn("growthAllBotsSummaryBody", html_body)
        self.assertIn("growthPerEaTablesGrid", html_body)

        # 2) Initially no Growth snapshot published
        code, g0 = self._req("/api/growth")
        self.assertEqual(code, 200)
        self.assertFalse(g0["has_data"])

        # 3) Admin gets auto-generated Growth Sync token & optionally locks MT5 login
        self._req("/admin/api/login", method="POST", body={"password": "SecretTestPassword!"}, role="admin")
        code, ov = self._req("/admin/api/overview", role="admin")
        self.assertEqual(code, 200)
        tok1 = ov["growth_sync"]["token"]
        self.assertTrue(tok1.startswith("ADG-"))

        code, set_res = self._req("/admin/api/settings", method="POST", body={"growth_sync_mt5_login": "777888"}, role="admin")
        self.assertEqual(code, 200)
        self.assertTrue(set_res["ok"])

        sample_growth = {
            "bot_order": ["zenith", "twoshots", "mox", "multiversus", "agnesa", "momentum", "albaniang"],
            "portfolio": {
                "bot_id": "portfolio",
                "name": "Portfolio Total",
                "subtitle": "All 7 EAs",
                "timeframe": "ALL",
                "magic": "ALL",
                "symbol": "xauusd.s",
                "info": {"gain_pct": 12.5, "abs_gain_pct": 12.5, "daily_pct": 0.4, "monthly_pct": 6.0, "drawdown_pct": 2.1, "balance": 1125.0, "equity": 1125.0, "equity_pct": 100.0, "highest_label": "2026-11-15", "highest_balance": 1125.0, "profit": 125.0, "interest": 0.0, "deposits": 1000.0, "withdrawals": 0.0, "updated": "2026-11-16 00:01"},
                "stats": {"trades": 14, "live_trades": 14, "win_rate": 78.6, "wins": 11, "losses": 3, "gross_profit": 160.0, "gross_loss": 35.0, "profit_factor": 4.57, "avg_win": 14.55, "avg_loss": -11.67, "best_trade": 32.0, "worst_trade": -15.0, "today_profit": 0.0, "month_profit": 125.0},
                "general": {"start_date": "2026-11-01", "period": "2026-11-01 — Today", "mode": "MT5 Live Balance"},
                "curve": [
                    {"date": "2026-11-01", "daily_profit": 0.0, "daily_pct": 0.0, "cum_profit": 0.0, "gain_pct": 0.0, "balance": 1000.0, "equity": 1000.0, "load_pct": 0.0, "trades": 0, "wins": 0, "losses": 0, "source": "MT5 Balance"},
                    {"date": "2026-11-15", "daily_profit": 45.0, "daily_pct": 4.17, "cum_profit": 125.0, "gain_pct": 12.5, "balance": 1125.0, "equity": 1125.0, "load_pct": 8.4, "trades": 3, "wins": 3, "losses": 0, "source": "Live MT5"},
                ],
            },
            "bots": {
                bid: {
                    "bot_id": bid,
                    "name": bid.upper(),
                    "subtitle": "EA",
                    "timeframe": "M5",
                    "magic": 100,
                    "symbol": "xauusd.s",
                    "info": {"gain_pct": 2.0, "abs_gain_pct": 2.0, "daily_pct": 0.2, "monthly_pct": 2.0, "drawdown_pct": 0.5, "balance": 1020.0, "equity": 1020.0, "equity_pct": 100.0, "highest_label": "2026-11-15", "highest_balance": 1020.0, "profit": 20.0, "interest": 0.0, "deposits": 1000.0, "withdrawals": 0.0, "updated": "2026-11-16 00:01"},
                    "stats": {"trades": 2, "live_trades": 2, "win_rate": 100.0, "wins": 2, "losses": 0, "gross_profit": 20.0, "gross_loss": 0.0, "profit_factor": 99.0, "avg_win": 10.0, "avg_loss": 0.0, "best_trade": 12.0, "worst_trade": 0.0, "today_profit": 0.0, "month_profit": 20.0},
                    "general": {"start_date": "2026-11-01", "period": "2026-11-01 — Today", "mode": "MT5 Live Balance"},
                    "curve": [{"date": "2026-11-15", "daily_profit": 20.0, "daily_pct": 2.0, "cum_profit": 20.0, "gain_pct": 2.0, "balance": 1020.0, "equity": 1020.0, "load_pct": 5.0, "trades": 2, "wins": 2, "losses": 0, "source": "Live MT5"}],
                }
                for bid in ["zenith", "twoshots", "mox", "multiversus", "agnesa", "momentum", "albaniang"]
            },
        }

        # 4) Wrong token is rejected 401; wrong MT5 login when locked is rejected 403
        code, _ = self._req("/api/growth/sync", method="POST", body={"sync_token": "BAD-TOKEN", "mt5_login": "777888", "growth": sample_growth})
        self.assertEqual(code, 401)

        code, _ = self._req("/api/growth/sync", method="POST", body={"sync_token": tok1, "mt5_login": "999000", "growth": sample_growth})
        self.assertEqual(code, 403)

        # 5) Valid sync saves snapshot & serves it on GET /api/growth
        code, s_ok = self._req("/api/growth/sync", method="POST", body={
            "sync_token": tok1,
            "mt5_login": "777888",
            "currency": "USD",
            "server_day": "2026-11-16",
            "sync_reason": "daily_24h",
            "growth": sample_growth,
        })
        self.assertEqual(code, 200)
        self.assertTrue(s_ok["ok"])

        code, g1 = self._req("/api/growth")
        self.assertEqual(code, 200)
        self.assertTrue(g1["has_data"])
        self.assertEqual(g1["meta"]["mt5_login"], "777888")
        self.assertAlmostEqual(g1["growth"]["portfolio"]["info"]["balance"], 1125.0, places=2)
        self.assertEqual(len(g1["growth"]["bot_order"]), 7)

        # 6) Presence ping on "growth" appears in Admin Live Visitors
        code, _ = self._req("/api/presence/ping", method="POST", body={"visitor_id": "v_growth_1", "tab_id": "tg_1", "page": "growth"})
        self.assertEqual(code, 200)
        code, lv_res = self._req("/admin/api/live-visitors", role="admin")
        self.assertEqual(lv_res["live_visitors"]["visitors"][0]["page"], "growth")

        # 7) Token rotation invalidates old token immediately
        code, rot = self._req("/admin/api/growth-sync/rotate-token", method="POST", body={}, role="admin")
        self.assertEqual(code, 200)
        tok2 = rot["token"]
        self.assertNotEqual(tok1, tok2)
        code, _ = self._req("/api/growth/sync", method="POST", body={"sync_token": tok1, "mt5_login": "777888", "growth": sample_growth})
        self.assertEqual(code, 401)
        code, s_ok2 = self._req("/api/growth/sync", method="POST", body={"sync_token": tok2, "mt5_login": "777888", "growth": sample_growth})
        self.assertEqual(code, 200)
        self.assertTrue(s_ok2["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
