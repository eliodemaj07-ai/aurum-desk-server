import http.cookiejar
import json
import os
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
            "PUBLIC_CONTACT_EMAIL": "demajelio132@gmail.com",
            "DEFAULT_DOWNLOAD_URL": "https://example.com/Aurum_Desk_Windows.zip",
        }
        self.mock_mailer = MockMailer(self.env)
        self.server = create_app_server(port=0, env_override=self.env, mailer_override=self.mock_mailer)
        self.mock_mailer.db = self.server.db
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        self.jar = http.cookiejar.CookieJar()
        self.admin_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _req(self, path, method="GET", body=None, use_admin=False):
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"} if body is not None else {}
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        opener = self.admin_opener if use_admin else urllib.request.build_opener()
        try:
            with opener.open(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

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
        with urllib.request.urlopen(self.base + "/") as r:
            store_html = r.read().decode("utf-8")
            self.assertIn("paypal.me/AurumDesk", store_html)
            self.assertIn("Aurum Desk", store_html)
        with urllib.request.urlopen(self.base + "/admin") as r:
            admin_html = r.read().decode("utf-8")
            self.assertIn("Live Notifications", admin_html)

        code, health = self._req("/health")
        self.assertEqual(code, 200)
        self.assertTrue(health["ok"])

        code, _ = self._req("/admin/api/overview", use_admin=True)
        self.assertEqual(code, 401)

        code, bad_login = self._req("/admin/api/login", method="POST", body={"password": "wrong"}, use_admin=True)
        self.assertEqual(code, 401)

        code, ok_login = self._req("/admin/api/login", method="POST", body={"password": "SecretTestPassword!"}, use_admin=True)
        self.assertEqual(code, 200)
        self.assertTrue(ok_login["ok"])

        code, ov = self._req("/admin/api/overview", use_admin=True)
        self.assertEqual(code, 200)
        self.assertTrue(ov["ok"])

    def test_03_full_paypal_order_approval_email_and_mt5_lock(self):
        # 1) Login as admin
        self._req("/admin/api/login", method="POST", body={"password": "SecretTestPassword!"}, use_admin=True)

        # 2) Customer submits PayPal order for Zenith + Albanian G (€110 Lifetime) on MT5 #55511122
        code, created = self._req("/api/orders", method="POST", body={
            "customer_name": "Alex Trader",
            "customer_email": "alex@example.com",
            "mt5_login": "55511122",
            "plan_type": "lifetime",
            "bot_ids": ["zenith", "albaniang"],
            "paypal_tx": "TXN-998877",
            "screenshot_data": "data:image/jpeg;base64,/9j/4AAQSkZJRg==",
        })
        self.assertEqual(code, 201)
        order = created["order"]
        self.assertEqual(order["amount_eur"], 110.0)
        self.assertEqual(order["status"], "pending")
        ref = order["order_ref"]

        # 3) Check Live Notification arrived in Admin Panel (no Telegram!)
        _, ov = self._req("/admin/api/overview", use_admin=True)
        self.assertEqual(ov["unread_count"], 1)
        self.assertEqual(ov["notifications"][0]["event_type"], "NEW_ORDER")
        self.assertEqual(ov["metrics"]["pending_orders"], 1)

        # 4) Admin approves order -> generates Unique Key & sends English email
        code, app_res = self._req(f"/admin/api/orders/{ref}/approve", method="POST", body={}, use_admin=True)
        self.assertEqual(code, 200)
        self.assertTrue(app_res["ok"])
        key = app_res["license"]["license_key"]
        self.assertTrue(key.startswith("AURUM-"))
        self.assertEqual(app_res["license"]["bound_mt5_login"], "55511122")
        self.assertEqual(app_res["license"]["bot_ids"], ["zenith", "albaniang"])

        # Customer email + admin copy sent in English
        cust_emails = [m for m in self.mock_mailer.sent if m["to"] == "alex@example.com"]
        self.assertEqual(len(cust_emails), 1)
        self.assertIn(key, cust_emails[0]["text"])
        self.assertIn("#55511122", cust_emails[0]["text"])
        self.assertIn("https://example.com/Aurum_Desk_Windows.zip", cust_emails[0]["text"])

        # 5) Public order tracker confirms approved without leaking full key
        code, trk = self._req(f"/api/order-status/{ref}")
        self.assertEqual(code, 200)
        self.assertEqual(trk["order"]["status"], "approved")
        self.assertNotIn("license_key", trk["order"])

        # 6) Desktop app verifies key BEFORE MT5 connects
        code, v0 = self._req("/api/license/verify", method="POST", body={"license_key": key, "mt5_login": ""})
        self.assertEqual(code, 200)
        self.assertTrue(v0["ok"])
        self.assertFalse(v0["mt5_verified"])
        self.assertEqual(v0["bound_mt5_login"], "55511122")

        # 7) Desktop app verifies key WITH matching MT5 #55511122 -> unlocks ONLY zenith & albaniang
        code, v1 = self._req("/api/license/verify", method="POST", body={"license_key": key, "mt5_login": "55511122"})
        self.assertEqual(code, 200)
        self.assertTrue(v1["ok"])
        self.assertTrue(v1["mt5_verified"])
        self.assertEqual(v1["allowed_bots"], ["zenith", "albaniang"])

        # 8) Another MT5 account (#99988877) tries the same Unique Key -> BLOCKED + Danger Alert in Admin!
        code, v_bad = self._req("/api/license/verify", method="POST", body={"license_key": key, "mt5_login": "99988877"})
        self.assertEqual(code, 403)
        self.assertFalse(v_bad["ok"])
        self.assertEqual(v_bad["error"], "account_mismatch")

        _, ov2 = self._req("/admin/api/overview", use_admin=True)
        self.assertEqual(ov2["notifications"][0]["event_type"], "MT5_MISMATCH")

        # 9) Admin rebinds license to new MT5 #77766655 and suspends/reactivates
        code, upd = self._req(f"/admin/api/licenses/{key}", method="POST", body={"bound_mt5_login": "77766655"}, use_admin=True)
        self.assertEqual(code, 200)
        self.assertEqual(upd["license"]["bound_mt5_login"], "77766655")

        code, v_old = self._req("/api/license/verify", method="POST", body={"license_key": key, "mt5_login": "55511122"})
        self.assertEqual(code, 403)
        code, v_new = self._req("/api/license/verify", method="POST", body={"license_key": key, "mt5_login": "77766655"})
        self.assertEqual(code, 200)
        self.assertTrue(v_new["mt5_verified"])

        # 10) Admin suspends license -> verify fails
        self._req(f"/admin/api/licenses/{key}", method="POST", body={"status": "suspended"}, use_admin=True)
        code, v_susp = self._req("/api/license/verify", method="POST", body={"license_key": key, "mt5_login": "77766655"})
        self.assertEqual(code, 400)
        self.assertEqual(v_susp["error"], "license_suspended")


if __name__ == "__main__":
    unittest.main(verbosity=2)
