import hashlib
import hmac
import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from app.billing.catalog import GB, PLANS, Tier, stripe_locale, to_minor_units
from app.billing.checkout import checkout_session_params, enterprise_invoice_params, price_create_params
from app.billing.entitlements import REFERRAL_BONUS_CAP_BYTES, compute_entitlements
from app.billing.webhooks import handle_event, process_webhook, verify_signature
from app.core.errors import NotAuthorized, ValidationFailed

UTC = timezone.utc
NOW = datetime(2030, 6, 1, tzinfo=UTC)
SECRET = "whsec_test"


def sign(payload: bytes, ts: int, secret=SECRET) -> str:
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


class FakeBilling:
    def __init__(self):
        self.seen, self.subs, self.audits, self.customers = set(), {}, [], {}

    def claim_event(self, eid, etype):
        if eid in self.seen:
            return False
        self.seen.add(eid)
        return True

    def tenant_for_customer(self, cust): return self.customers.get(cust)

    def upsert_subscription(self, tenant, **f): self.subs.setdefault(tenant, {}).update(f)

    def audit(self, tenant, et, payload): self.audits.append(et)


class CatalogTests(unittest.TestCase):
    def test_minor_units_and_zero_decimal(self):
        self.assertEqual(to_minor_units("USD", Decimal("4.99")), 499)
        self.assertEqual(to_minor_units("JPY", Decimal("690")), 690)
        self.assertEqual(to_minor_units("INR", Decimal("199")), 19900)

    def test_spec_prices_and_quotas(self):
        self.assertEqual(PLANS["premium_monthly"].minor("USD"), 499)
        self.assertEqual(PLANS["lifetime"].minor("USD"), 14900)
        self.assertEqual(PLANS["free"].storage_bytes, 1 * GB)
        self.assertEqual(PLANS["lifetime"].storage_bytes, 50 * GB)

    def test_every_plan_has_usd_and_yearly_is_cheaper_than_12_months(self):
        for p in PLANS.values():
            self.assertIn("USD", p.prices)
        self.assertLess(PLANS["premium_yearly"].prices["USD"], PLANS["premium_monthly"].prices["USD"] * 12)

    def test_locale_mapping(self):
        self.assertEqual(stripe_locale("tl"), "fil")
        self.assertEqual(stripe_locale("ar"), "auto")


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.t, self.u = uuid4(), uuid4()

    def params(self, plan, **kw):
        return checkout_session_params(plan_key=plan, price_id="price_x", tenant_id=self.t, user_id=self.u,
                                       success_url="https://app.test/ok", cancel_url="https://app.test/no", **kw)

    def test_global_payment_settings(self):
        p = self.params("premium_monthly", locale="pt-BR", referral_code="ABCD2345")
        self.assertNotIn("payment_method_types", p)               # would disable local methods
        self.assertTrue(p["adaptive_pricing"]["enabled"])
        self.assertTrue(p["automatic_tax"]["enabled"])
        self.assertTrue(p["tax_id_collection"]["enabled"])
        self.assertEqual((p["mode"], p["locale"]), ("subscription", "pt-BR"))
        self.assertEqual(p["subscription_data"]["metadata"]["tenant_id"], str(self.t))

    def test_lifetime_is_one_time_payment_with_invoice(self):
        p = self.params("lifetime")
        self.assertEqual(p["mode"], "payment")
        self.assertTrue(p["invoice_creation"]["enabled"])

    def test_rejects_free_and_insecure_urls(self):
        with self.assertRaises(ValidationFailed):
            self.params("free")
        with self.assertRaises(ValidationFailed):
            checkout_session_params(plan_key="premium_monthly", price_id="p", tenant_id=self.t, user_id=self.u,
                                    success_url="http://x", cancel_url="https://y")

    def test_price_params_include_local_currencies(self):
        pp = price_create_params(PLANS["premium_monthly"], "prod_1")
        self.assertEqual(pp["unit_amount"], 499)
        self.assertEqual(pp["currency_options"]["jpy"]["unit_amount"], 690)
        self.assertEqual(pp["recurring"], {"interval": "month"})
        self.assertNotIn("recurring", price_create_params(PLANS["lifetime"], "prod_1"))

    def test_enterprise_minimum_and_terms(self):
        with self.assertRaises(ValidationFailed):
            enterprise_invoice_params(customer_id="cus_1", tenant_id=self.t, annual_usd=Decimal("4999"))
        p = enterprise_invoice_params(customer_id="cus_1", tenant_id=self.t, annual_usd=Decimal("5000"),
                                      po_number="PO-77")
        self.assertEqual((p["collection_method"], p["days_until_due"]), ("send_invoice", 30))
        self.assertEqual(p["items"][0]["price_data"]["unit_amount"], 500000)


class SignatureTests(unittest.TestCase):
    body = b'{"id":"evt_1"}'

    def test_valid(self):
        verify_signature(self.body, sign(self.body, 1000), SECRET, now=1100)

    def test_tampered_body_wrong_secret_stale_and_malformed(self):
        with self.assertRaises(NotAuthorized):
            verify_signature(self.body + b" ", sign(self.body, 1000), SECRET, now=1000)
        with self.assertRaises(NotAuthorized):
            verify_signature(self.body, sign(self.body, 1000, "whsec_other"), SECRET, now=1000)
        with self.assertRaises(NotAuthorized):
            verify_signature(self.body, sign(self.body, 1000), SECRET, now=1000 + 301)
        for bad in ("", "garbage", "t=abc,v1=00", "t=1000"):
            with self.assertRaises(NotAuthorized):
                verify_signature(self.body, bad, SECRET, now=1000)

    def test_any_of_multiple_v1_signatures_accepted(self):
        good = sign(self.body, 1000)
        verify_signature(self.body, good + ",v1=deadbeef", SECRET, now=1000)


def evt(eid, etype, obj): return {"id": eid, "type": etype, "data": {"object": obj}}


class WebhookHandlingTests(unittest.TestCase):
    def setUp(self):
        self.s, self.t = FakeBilling(), uuid4()

    def test_lifetime_purchase(self):
        e = evt("e1", "checkout.session.completed", {
            "mode": "payment", "payment_status": "paid", "customer": "cus_1", "currency": "inr",
            "metadata": {"tenant_id": str(self.t), "plan": "lifetime"}})
        r = handle_event(e, self.s, now=NOW)
        self.assertTrue(r.handled)
        sub = self.s.subs[self.t]
        self.assertEqual((sub["tier"], sub["is_lifetime"], sub["currency"]), ("lifetime", True, "INR"))
        self.assertEqual(sub["storage_quota_bytes"], 50 * GB)

    def test_duplicate_delivery_is_ignored(self):
        e = evt("e2", "customer.subscription.updated", {"status": "active", "metadata": {
            "tenant_id": str(self.t), "plan": "premium_monthly"}})
        self.assertTrue(handle_event(e, self.s, now=NOW).handled)
        self.assertEqual(handle_event(e, self.s, now=NOW).reason, "duplicate")

    def test_unpaid_delayed_lifetime_waits(self):
        e = evt("e3", "checkout.session.completed", {"mode": "payment", "payment_status": "unpaid",
                "metadata": {"tenant_id": str(self.t), "plan": "lifetime"}})
        self.assertEqual(handle_event(e, self.s, now=NOW).reason, "lifetime_not_paid_yet")
        self.assertNotIn("is_lifetime", self.s.subs.get(self.t, {}))

    def test_subscription_lifecycle_and_customer_lookup(self):
        self.s.customers["cus_9"] = self.t
        handle_event(evt("e4", "customer.subscription.created", {
            "status": "active", "customer": "cus_9", "id": "sub_1",
            "items": {"data": [{"price": {"lookup_key": "premium_yearly"}}]}}), self.s, now=NOW)
        self.assertEqual(self.s.subs[self.t]["tier"], "premium")
        handle_event(evt("e5", "invoice.payment_failed", {"customer": "cus_9", "id": "in_1"}), self.s, now=NOW)
        self.assertEqual(self.s.subs[self.t]["status"], "past_due")
        handle_event(evt("e6", "customer.subscription.deleted", {"customer": "cus_9"}), self.s, now=NOW)
        self.assertEqual(self.s.subs[self.t]["status"], "canceled")

    def test_unknown_events_and_unresolvable_tenants(self):
        self.assertEqual(handle_event(evt("e7", "charge.succeeded", {}), self.s, now=NOW).reason, "ignored")
        r = handle_event(evt("e8", "customer.subscription.updated", {"status": "active"}), self.s, now=NOW)
        self.assertEqual((r.reason, r.retry), ("no_tenant", True))
        self.assertNotIn("e8", self.s.seen)          # not claimed, so Stripe's redelivery still works

    def test_end_to_end_signed_webhook(self):
        body = json.dumps(evt("e9", "customer.subscription.updated", {
            "status": "active", "metadata": {"tenant_id": str(self.t), "plan": "premium_monthly"}})).encode()
        import time
        r = process_webhook(body, sign(body, int(time.time())), SECRET, self.s)
        self.assertTrue(r.handled)
        with self.assertRaises(NotAuthorized):
            process_webhook(body, "t=1,v1=00", SECRET, self.s)


class EntitlementTests(unittest.TestCase):
    def ent(self, **kw):
        base = dict(tier=Tier.PREMIUM, status="active", storage_quota_bytes=None, used_bytes=0, now=NOW)
        base.update(kw)
        return compute_entitlements(**base)

    def test_free_vs_premium_vs_lifetime(self):
        self.assertEqual(self.ent(tier=Tier.FREE, status="active").storage_quota_bytes, 1 * GB)
        self.assertEqual(self.ent().storage_quota_bytes, 50 * GB)
        e = self.ent(tier=Tier.FREE, is_lifetime=True)
        self.assertEqual((e.tier, e.storage_quota_bytes), (Tier.LIFETIME, 50 * GB))

    def test_lapse_downgrades_creation_but_never_blocks_existing_data(self):
        e = self.ent(status="canceled", used_bytes=10 * GB)
        self.assertEqual(e.tier, Tier.FREE)
        self.assertFalse(e.can_create)            # over free quota: no NEW uploads
        self.assertNotIn("liveness", e.triggers)  # (data itself is untouched: nothing here deletes)

    def test_past_due_grace_period(self):
        soon = self.ent(status="past_due", past_due_since=NOW - timedelta(days=5))
        late = self.ent(status="past_due", past_due_since=NOW - timedelta(days=15))
        self.assertEqual(soon.tier, Tier.PREMIUM)
        self.assertEqual(late.tier, Tier.FREE)

    def test_referral_bonus_is_capped(self):
        e = self.ent(tier=Tier.FREE, status="active", referral_bonus_bytes=10**15)
        self.assertEqual(e.storage_quota_bytes, 1 * GB + REFERRAL_BONUS_CAP_BYTES)

    def test_enterprise_gets_the_corporate_feature_set(self):
        e = self.ent(tier=Tier.ENTERPRISE, storage_quota_bytes=5000 * GB)
        self.assertTrue(all([e.white_label, e.sso, e.audit_export, e.data_residency]))
        self.assertEqual(e.max_sub_capsules, 50_000)


if __name__ == "__main__":
    unittest.main()


class PriceBookSyncTests(unittest.TestCase):
    def test_web_price_book_matches_backend(self):
        import json
        import pathlib
        from scripts.export_price_book import build
        committed = json.loads((pathlib.Path(__file__).parents[3] / "apps/web/src/lib/price-book.json").read_text())
        self.assertEqual(committed, build(), "run scripts/export_price_book.py and commit the result")
