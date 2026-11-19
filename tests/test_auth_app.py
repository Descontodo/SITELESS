import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
import auth
from auth import hash_password, verify_password


class AuthPrimitiveTests(unittest.TestCase):
    def test_password_hash_is_salted_and_verifiable(self):
        with patch.object(auth, "PASSWORD_ITERATIONS", 1000):
            first = hash_password("strong-password-123")
            second = hash_password("strong-password-123")
        self.assertNotEqual(first, second)
        self.assertTrue(verify_password("strong-password-123", first))
        self.assertFalse(verify_password("wrong-password", first))


class AccountIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(app, "DB_PATH", Path(self.temp_dir.name) / "test.db")
        self.db_patch.start()
        self.iterations_patch = patch.object(auth, "PASSWORD_ITERATIONS", 1000)
        self.iterations_patch.start()
        app.init_db()
        self.client = TestClient(app.app)

    def tearDown(self):
        self.client.close()
        self.iterations_patch.stop()
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_registration_creates_free_session_and_admin_routes_are_private(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Starter — $19/month", page.text)
        self.assertIn("Agency — $49/month", page.text)
        self.assertNotIn("R$ 47/mês", page.text)
        self.assertNotIn("R$ 97/mês", page.text)
        response = self.client.post("/api/auth/register", json={
            "email": "prospect@example.test", "password": "strong-password-123"
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["quota"]["plan"], "free")
        self.assertIn("httponly", response.headers["set-cookie"].lower())
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)
        self.assertEqual(self.client.get("/api/admin/users").status_code, 403)
        self.assertEqual(self.client.get("/api/export/xlsx").status_code, 403)
        csv_response = self.client.get("/api/export/csv")
        self.assertEqual(csv_response.status_code, 200)
        self.assertTrue(csv_response.content.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(csv_response.content.decode("utf-8-sig").splitlines()[0], ",".join(app.LEAD_COLUMNS))

    def test_admin_login_and_plan_controls(self):
        self.client.post("/api/auth/register", json={
            "email": "prospect@example.test", "password": "strong-password-123"
        })
        with TestClient(app.app) as client:
            login = client.post("/api/auth/login", json={
                "email": app.ADMIN_EMAIL, "password": app.ADMIN_PASSWORD
            })
            self.assertEqual(login.status_code, 200)
            self.assertEqual(login.json()["quota"]["plan"], "admin")
            self.assertEqual(client.get("/admin").status_code, 200)
            listing = client.get("/api/admin/users")
            self.assertEqual(listing.status_code, 200)
            user_id = next(row["id"] for row in listing.json()["items"] if row["email"] == "prospect@example.test")
            changed = client.patch(f"/api/admin/users/{user_id}/plan", json={"plan": "starter"})
            self.assertEqual(changed.status_code, 200)
        self.assertEqual(self.client.get("/api/export/xlsx").status_code, 200)

    def test_leads_are_deduplicated_and_isolated_per_account(self):
        registration = self.client.post("/api/auth/register", json={
            "email": "owner@example.test", "password": "strong-password-456"
        })
        owner_id = registration.json()["id"]
        lead = {
            "nome": "Clínica Exemplo", "nicho": "dentista", "endereco": "Rua A, 10",
            "cidade": "Lisboa", "pais": "Portugal", "telefone": "+351 210 000 000",
            "email": "contato@example.test", "motivo_flag": "sem website",
        }
        self.assertEqual(app.save_lead(lead, owner_id), (True, False))
        self.assertEqual(app.save_lead({**lead, "nome": " clínica   exemplo ", "instagram": "https://instagram.com/exemplo"}, owner_id), (False, False))
        rows, count = app.list_leads(owner_id, query="Clínica", limit=None)
        self.assertEqual(count, 1)
        self.assertEqual(rows[0]["instagram"], "https://instagram.com/exemplo")
        with closing(app._connect()) as connection:
            other_id = connection.execute(
                "INSERT INTO users(email,password_hash,period_started,created_at) VALUES(?,?,?,?)",
                ("other@example.test", hash_password("other-password-789"), app.utc_now().isoformat(), app.utc_now().isoformat()),
            ).lastrowid
        self.assertEqual(app.list_leads(other_id, limit=None), ([], 0))

    def test_free_contact_lock_and_admin_unlimited_quota(self):
        registration = self.client.post("/api/auth/register", json={
            "email": "free@example.test", "password": "strong-password-987"
        })
        free_id = registration.json()["id"]
        for number in range(6):
            self.assertEqual(app.save_lead({
                "nome": f"Empresa {number}", "endereco": f"Rua {number}",
                "email": f"contact{number}@example.test", "motivo_flag": "sem website",
            }, free_id), (True, False))
        records, _ = app.list_leads(free_id, limit=None)
        visible = app._public_leads({"id": free_id, "plan": "free", "role": "user"}, records)
        self.assertEqual(sum(bool(row["contacts_locked"]) for row in visible), 1)
        self.assertEqual(sum(bool(row["email"]) for row in visible), 5)

        with closing(app._connect()) as connection:
            admin_id = int(connection.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0])
            connection.execute("UPDATE users SET usage_count=999999 WHERE id=?", (admin_id,))
            connection.commit()
        created, exhausted = app.save_lead({"nome": "Admin Lead", "motivo_flag": "sem website"}, admin_id)
        self.assertTrue(created)
        self.assertFalse(exhausted)

    def test_quota_renews_after_thirty_day_period(self):
        response = self.client.post("/api/auth/register", json={
            "email": "renew@example.test", "password": "strong-password-321"
        })
        user_id = response.json()["id"]
        old_period = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        with closing(app._connect()) as connection:
            connection.execute("UPDATE users SET period_started=?,usage_count=15 WHERE id=?", (old_period, user_id))
            connection.commit()
        profile = self.client.get("/api/auth/me").json()
        self.assertEqual(profile["quota"]["used"], 0)
        self.assertEqual(profile["quota"]["remaining"], 15)

    def test_stripe_webhook_activates_plan_idempotently(self):
        registration = self.client.post("/api/auth/register", json={
            "email": "paid@example.test", "password": "strong-password-654"
        })
        event = {
            "id": "evt-test-1",
            "type": "checkout.session.completed",
            "data": {"object": {
                "mode": "subscription", "payment_status": "paid", "customer": "cus-test-1",
                "subscription": "sub-test-1", "client_reference_id": str(registration.json()["id"]),
                "metadata": {"user_id": str(registration.json()["id"]), "plan": "starter"},
            }},
        }
        headers = {"Stripe-Signature": "sig-test"}
        with patch.object(app, "STRIPE_WEBHOOK_SECRET", "test-secret"), \
             patch.object(app, "construct_webhook_event", return_value=event):
            activated = self.client.post("/api/webhook/stripe", content=b"stripe-event", headers=headers)
            duplicate = self.client.post("/api/webhook/stripe", content=b"stripe-event", headers=headers)
        with patch.object(app, "STRIPE_WEBHOOK_SECRET", "test-secret"), \
             patch.object(app, "construct_webhook_event", side_effect=app.InvalidStripeWebhook("bad signature")):
            denied = self.client.post("/api/webhook/stripe", content=b"invalid", headers=headers)
        self.assertEqual(activated.status_code, 200)
        self.assertTrue(duplicate.json()["duplicate"])
        self.assertEqual(denied.status_code, 400)
        self.assertEqual(self.client.get("/api/auth/me").json()["quota"]["plan"], "starter")

    def test_checkout_stays_disabled_until_secret_and_webhook_are_configured(self):
        self.client.post("/api/auth/register", json={
            "email": "checkout@example.test", "password": "strong-password-777"
        })
        with patch.object(app, "STRIPE_SECRET_KEY", ""), patch.object(app, "STRIPE_WEBHOOK_SECRET", ""):
            missing_key = self.client.post("/api/billing/checkout", json={"plan": "starter"})
        self.assertEqual(missing_key.status_code, 503)
        self.assertIn("STRIPE_SECRET_KEY", missing_key.json()["detail"])
        with patch.object(app, "STRIPE_SECRET_KEY", "sk_live_placeholder"), patch.object(app, "STRIPE_WEBHOOK_SECRET", ""):
            missing_webhook = self.client.post("/api/billing/checkout", json={"plan": "starter"})
        self.assertEqual(missing_webhook.status_code, 503)
        self.assertIn("webhook", missing_webhook.json()["detail"])


if __name__ == "__main__":
    unittest.main()
