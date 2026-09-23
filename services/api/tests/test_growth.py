import unittest
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from app.billing.catalog import GB
from app.billing.entitlements import REFERRAL_BONUS_CAP_BYTES
from app.core.errors import Conflict, NotAuthorized, QuotaExceeded, ValidationFailed
from app.growth.event_pages import EventPage, authorize_upload, join_url, new_token, verify_token
from app.growth.provisioning import MAX_ROWS, neutralise, parse_csv
from app.growth.referrals import (Referral, Status, VELOCITY_LIMIT_PER_DAY, new_code, normalise_code,
                                  qualify_referral, register_referral)
from app.growth.teaser import build_teaser, sanitize_public_message

UTC = timezone.utc
NOW = datetime(2030, 1, 1, tzinfo=UTC)


class FakeRefStore:
    def __init__(self):
        self.codes, self.refs, self.bonus, self.recent = {}, {}, {}, 0

    def tenant_for_code(self, code): return self.codes.get(code)
    def get_for_referee(self, t): return self.refs.get(t)
    def create(self, r): self.refs[r.referee_tenant] = r
    def set_status(self, t, s, now):
        r = self.refs[t]
        self.refs[t] = Referral(r.referrer_tenant, r.referee_tenant, s, r.referrer_email_hash,
                                r.referee_email_hash, r.referrer_device_hash, r.referee_device_hash)
    def bonus_bytes(self, t): return self.bonus.get(t, 0)
    def add_bonus(self, t, d): self.bonus[t] = self.bonus.get(t, 0) + d
    def count_since(self, t, since): return self.recent


class ReferralTests(unittest.TestCase):
    def setUp(self):
        self.s, self.referrer, self.referee = FakeRefStore(), uuid4(), uuid4()
        self.code = new_code()
        self.s.codes[self.code] = (self.referrer, "hash-a", "dev-a")

    def reg(self, **kw):
        a = dict(code=self.code, referee_tenant=self.referee, referee_email_hash="hash-b",
                 referee_device_hash="dev-b", now=NOW)
        a.update(kw)
        return register_referral(self.s, **a)

    def test_codes_are_well_formed_and_normalised(self):
        self.assertEqual(len(new_code()), 8)
        self.assertEqual(normalise_code(" abcd-2345 "), "ABCD2345")
        with self.assertRaises(ValidationFailed):
            normalise_code("ABCD0OIL")

    def test_signup_gives_welcome_bonus_only_referee_first(self):
        self.reg()
        self.assertEqual(self.s.bonus[self.referee], 1 * GB)
        self.assertNotIn(self.referrer, self.s.bonus)

    def test_reward_requires_verified_email_and_sealed_vault(self):
        self.reg()
        self.assertFalse(qualify_referral(self.s, referee_tenant=self.referee, email_verified=True,
                                          sealed_first_vault=False, now=NOW))
        self.assertFalse(qualify_referral(self.s, referee_tenant=self.referee, email_verified=False,
                                          sealed_first_vault=True, now=NOW))
        self.assertTrue(qualify_referral(self.s, referee_tenant=self.referee, email_verified=True,
                                         sealed_first_vault=True, now=NOW))
        self.assertEqual(self.s.bonus[self.referrer], 1 * GB)

    def test_reward_is_idempotent(self):
        self.reg()
        args = dict(referee_tenant=self.referee, email_verified=True, sealed_first_vault=True, now=NOW)
        self.assertTrue(qualify_referral(self.s, **args))
        self.assertFalse(qualify_referral(self.s, **args))
        self.assertEqual(self.s.bonus[self.referrer], 1 * GB)

    def test_self_referral_blocked_by_tenant_email_and_device(self):
        with self.assertRaises(ValidationFailed):
            self.reg(referee_tenant=self.referrer)
        with self.assertRaises(ValidationFailed):
            self.reg(referee_email_hash="hash-a")
        with self.assertRaises(ValidationFailed):
            self.reg(referee_device_hash="dev-a")

    def test_cannot_use_two_codes_and_unknown_code(self):
        self.reg()
        with self.assertRaises(Conflict):
            self.reg()
        with self.assertRaises(ValidationFailed):
            self.reg(code="ZZZZZZZZ", referee_tenant=uuid4())

    def test_velocity_limit_holds_referral_without_bonus(self):
        self.s.recent = VELOCITY_LIMIT_PER_DAY
        r = self.reg()
        self.assertEqual(r.status, Status.HELD)
        self.assertNotIn(self.referee, self.s.bonus)
        self.assertFalse(qualify_referral(self.s, referee_tenant=self.referee, email_verified=True,
                                          sealed_first_vault=True, now=NOW))

    def test_bonus_cap_is_respected(self):
        self.s.bonus[self.referrer] = REFERRAL_BONUS_CAP_BYTES - GB // 2
        self.reg()
        qualify_referral(self.s, referee_tenant=self.referee, email_verified=True, sealed_first_vault=True, now=NOW)
        self.assertEqual(self.s.bonus[self.referrer], REFERRAL_BONUS_CAP_BYTES)


class TeaserTests(unittest.TestCase):
    def row(self, **kw):
        base = dict(teaser_enabled=True, state="sealed", unlock_at=NOW + timedelta(days=400),
                    teaser_message="See you in 2031", owner_email="secret@x.com", total_size_bytes=123456)
        base.update(kw)
        return base

    def test_countdown_and_no_leakage(self):
        t = build_teaser(self.row(), NOW)
        self.assertEqual((t.state, t.seconds_remaining), ("sealed", 400 * 86400))
        self.assertTrue(t.noindex)
        blob = repr(t)
        for leaked in ("secret@x.com", "123456"):
            self.assertNotIn(leaked, blob)

    def test_opt_in_and_revoked(self):
        with self.assertRaises(ValidationFailed):
            build_teaser(self.row(teaser_enabled=False), NOW)
        with self.assertRaises(ValidationFailed):
            build_teaser(self.row(state="revoked"), NOW)

    def test_open_state_and_message_rules(self):
        self.assertEqual(build_teaser(self.row(unlock_at=NOW - timedelta(days=1)), NOW).state, "open")
        with self.assertRaises(ValidationFailed):
            sanitize_public_message("x" * 141)
        self.assertEqual(sanitize_public_message("hi\x00 there"), "hi there")
        self.assertIsNone(sanitize_public_message("   "))

    def test_og_description_is_html_escaped(self):
        t = build_teaser(self.row(teaser_message="<script>alert(1)</script>"), NOW)
        self.assertNotIn("<script>", t.og_description)


class EventPageTests(unittest.TestCase):
    def setUp(self):
        self.token, h = new_token()
        self.page = EventPage("smith-wedding", h, NOW - timedelta(hours=1), NOW + timedelta(hours=8), True,
                              200 * 1024 * 1024, 5 * GB, used_event_bytes=0)

    def up(self, **kw):
        a = dict(presented_token=self.token, now=NOW, is_authenticated=False, content_type="image/jpeg",
                 size_bytes=3_000_000, guest_used_bytes=0, requests_last_minute=1)
        a.update(kw)
        authorize_upload(self.page, **a)

    def test_happy_path_and_token_hashing(self):
        self.up()
        self.assertTrue(verify_token(self.token, self.page.token_hash))
        self.assertFalse(verify_token("nope", self.page.token_hash))
        self.assertNotIn(self.token.encode(), self.page.token_hash)

    def test_each_rule(self):
        with self.assertRaises(NotAuthorized): self.up(presented_token="wrong")
        with self.assertRaises(NotAuthorized): self.up(now=NOW + timedelta(days=1))
        with self.assertRaises(NotAuthorized): self.up(now=NOW - timedelta(days=1))
        with self.assertRaises(QuotaExceeded): self.up(requests_last_minute=30)
        with self.assertRaises(ValidationFailed): self.up(content_type="application/x-msdownload")
        with self.assertRaises(ValidationFailed): self.up(size_bytes=0)
        with self.assertRaises(QuotaExceeded): self.up(guest_used_bytes=199 * 1024 * 1024, size_bytes=5_000_000)

    def test_members_only_events(self):
        self.page = EventPage(self.page.slug, self.page.token_hash, self.page.opens_at, self.page.closes_at,
                              False, self.page.max_guest_bytes, self.page.max_event_bytes)
        with self.assertRaises(NotAuthorized): self.up()
        self.up(is_authenticated=True)

    def test_join_url_keeps_token_in_fragment(self):
        u = join_url("https://vaultla.io", "smith-wedding", "TOK", "es")
        self.assertEqual(u, "https://vaultla.io/es/e/smith-wedding#t=TOK")
        with self.assertRaises(ValidationFailed): join_url("http://x", "s", "t")


CSV = "email,name,unlock_date,group\n"


class ProvisioningTests(unittest.TestCase):
    today = date(2030, 1, 1)

    def parse(self, body, **kw):
        return parse_csv((CSV + body).encode(), today=self.today, **kw)

    def test_valid_rows(self):
        r = self.parse("a@uni.edu,Ana,2040-05-01,Class of 2030\nb@uni.edu,Ben,2050-01-01,\n")
        self.assertTrue(r.ok)
        self.assertEqual((len(r.rows), r.rows[0].group, r.rows[1].group), (2, "Class of 2030", None))

    def test_row_level_errors_are_reported_with_row_numbers(self):
        r = self.parse("bad-email,Ana,2040-05-01,\na@uni.edu,,2040-05-01,\nc@uni.edu,Cy,2020-01-01,\n"
                       "d@uni.edu,Di,05/01/2040,\na@uni.edu,Dup,2040-01-01,\n")
        got = {(e.row, e.field) for e in r.errors}
        self.assertTrue({(2, "email"), (3, "name"), (4, "unlock_date"), (5, "unlock_date"), (6, "email")} <= got)
        self.assertFalse(r.ok)

    def test_formula_injection_is_neutralised(self):
        r = self.parse('a@uni.edu,"=HYPERLINK(""http://evil"")",2040-05-01,+cmd\n')
        self.assertTrue(r.rows[0].name.startswith("'="))
        self.assertTrue(r.rows[0].group.startswith("'+"))
        self.assertEqual(neutralise("safe"), "safe")

    def test_fatal_conditions(self):
        self.assertIn("Missing", parse_csv(b"email,name\na@b.co,x\n", today=self.today).fatal)
        self.assertIn("UTF-8", parse_csv(b"\xff\xfe\x00", today=self.today).fatal)
        self.assertIn("No data", parse_csv(CSV.encode(), today=self.today).fatal)
        self.assertIsNotNone(parse_csv(b"", today=self.today).fatal)

    def test_row_cap_and_plan_capacity(self):
        rows = "".join(f"u{i}@x.co,N{i},2040-01-01,\n" for i in range(51))
        self.assertIn("Row limit", self.parse(rows, capacity_remaining=50).fatal)
        big = "".join(f"u{i}@x.co,N{i},2040-01-01,\n" for i in range(MAX_ROWS))
        self.assertTrue(self.parse(big).ok)                      # exactly 50,000 is allowed
        self.assertIn("Row limit", self.parse(big + "z@x.co,Z,2040-01-01,\n").fatal)

    def test_bom_and_case_insensitive_headers(self):
        r = parse_csv("\ufeffEmail,NAME,Unlock_Date\na@uni.edu,Ana,2040-05-01\n".encode("utf-8"), today=self.today)
        self.assertTrue(r.ok)


if __name__ == "__main__":
    unittest.main()
