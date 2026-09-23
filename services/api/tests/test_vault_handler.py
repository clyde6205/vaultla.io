import base64
import hashlib
import unittest
from datetime import datetime, timezone
from uuid import uuid4

from app.core.errors import (IntegrityError, NotAuthorized, NotFound, QuotaExceeded,
                             ValidationFailed, VaultStillSealed)
from app.vault.handler import CreateVaultIn, ItemIn, VaultHandler, WrappedShareIn
from tests.fakes import FakeKms, FakeStore, FakeVaultRepo, FixedClock

UTC = timezone.utc
NOW = datetime(2030, 1, 1, tzinfo=UTC)
b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731


def make_item(data=b"ciphertext-bytes", share=b"\x01" + b"S" * 32):
    return data, ItemIn(len(data), hashlib.sha256(data).hexdigest(), b64(share),
                        [WrappedShareIn("owner", None, b64(b"w" * 60)),
                         WrappedShareIn("guardian", None, b64(b"g" * 60))])


class Base(unittest.TestCase):
    def setUp(self):
        self.clock, self.repo, self.store = FixedClock(NOW), FakeVaultRepo(), FakeStore()
        self.h = VaultHandler(self.repo, self.store, FakeKms(), self.clock)
        self.tenant, self.owner, self.other = uuid4(), uuid4(), uuid4()

    def create(self, trigger="fixed_date", cfg=None, n=1):
        cfg = cfg or {"unlock_at": "2040-01-01T00:00:00+00:00"}
        pairs = [make_item(f"data-{i}".encode()) for i in range(n)]
        out = self.h.create_vault(CreateVaultIn(self.tenant, self.owner, trigger, cfg, 1,
                                                [p[1] for p in pairs]))
        keys = {i.release_rank: self.repo.items[out.vault_id][i.release_rank].storage_key for i in out.items}
        for rank, (data, _) in enumerate(pairs):
            self.store.put(keys[rank], data)
        return out

    def seal(self, out):
        return self.h.finalize_vault(self.tenant, out.vault_id, self.owner)


class CreateTests(Base):
    def test_server_never_stores_plain_share(self):
        share = bytes(range(33))                       # non-palindromic so sealing is observable
        data, item = make_item(share=share)
        out = self.h.create_vault(CreateVaultIn(self.tenant, self.owner, "fixed_date",
                                  {"unlock_at": "2040-01-01T00:00:00+00:00"}, 1, [item]))
        stored = self.repo.items[out.vault_id][0]
        self.assertNotIn(share, stored.server_share_sealed)
        self.assertEqual(out.items[0].upload.headers["x-amz-checksum-sha256"], b64(stored.sha256))

    def test_rejects_past_fixed_date(self):
        with self.assertRaises(ValidationFailed):
            self.create(cfg={"unlock_at": "2029-01-01T00:00:00+00:00"})

    def test_rejects_bad_share_length_and_checksum(self):
        data, item = make_item()
        bad = ItemIn(item.size_bytes, item.sha256_hex, b64(b"short"), item.wrapped_shares)
        with self.assertRaises(ValidationFailed):
            self.h.create_vault(CreateVaultIn(self.tenant, self.owner, "fixed_date",
                                {"unlock_at": "2040-01-01T00:00:00+00:00"}, 1, [bad]))
        bad2 = ItemIn(item.size_bytes, "ZZ", item.server_share_b64, item.wrapped_shares)
        with self.assertRaises(ValidationFailed):
            self.h.create_vault(CreateVaultIn(self.tenant, self.owner, "fixed_date",
                                {"unlock_at": "2040-01-01T00:00:00+00:00"}, 1, [bad2]))

    def test_rejects_unknown_envelope_version_and_quota(self):
        _, item = make_item()
        with self.assertRaises(ValidationFailed):
            self.h.create_vault(CreateVaultIn(self.tenant, self.owner, "fixed_date",
                                {"unlock_at": "2040-01-01T00:00:00+00:00"}, 99, [item]))
        self.repo.quota = 1
        with self.assertRaises(QuotaExceeded):
            self.create()


class FinalizeTests(Base):
    def test_seal_happy_path(self):
        out = self.create()
        v = self.seal(out)
        self.assertEqual(v.state, "sealed")

    def test_missing_or_tampered_object_blocks_seal(self):
        out = self.create()
        key = self.repo.items[out.vault_id][0].storage_key
        self.store.objects[key] = b"tampered!!!!!!!!"          # same length class, different bytes
        with self.assertRaises(IntegrityError):
            self.seal(out)
        del self.store.objects[key]
        with self.assertRaises(IntegrityError):
            self.seal(out)

    def test_only_owner_can_seal(self):
        out = self.create()
        with self.assertRaises(NotAuthorized):
            self.h.finalize_vault(self.tenant, out.vault_id, self.other)


class ReleaseTests(Base):
    def test_sealed_vault_refuses_release_even_to_owner(self):
        out = self.create()
        self.seal(out)
        with self.assertRaises(VaultStillSealed):
            self.h.release_server_share(self.tenant, out.vault_id, out.items[0].item_id, self.owner)

    def test_matured_vault_releases_to_owner_and_recipient_only(self):
        out = self.create()
        self.seal(out)
        self.repo.vaults[out.vault_id]["state"] = "matured"     # as Chronos would
        r = self.h.release_server_share(self.tenant, out.vault_id, out.items[0].item_id, self.owner)
        self.assertEqual(base64.b64decode(r.share_b64), b"\x01" + b"S" * 32)
        with self.assertRaises(NotAuthorized):
            self.h.release_server_share(self.tenant, out.vault_id, out.items[0].item_id, self.other)
        self.repo.recipients.add((out.vault_id, self.other))
        self.h.release_server_share(self.tenant, out.vault_id, out.items[0].item_id, self.other)

    def test_cross_tenant_access_looks_like_not_found(self):
        out = self.create()
        with self.assertRaises(NotFound):
            self.h.release_server_share(uuid4(), out.vault_id, out.items[0].item_id, self.owner)

    def test_progressive_releases_only_earned_items(self):
        cfg = {"start_at": "2030-06-01T00:00:00+00:00", "step_bps": 2500}     # 25% per year
        out = self.create("progressive", cfg, n=4)
        self.seal(out)
        self.repo.vaults[out.vault_id]["bps"] = 2500       # Chronos advanced one tranche
        ok = out.items[0].item_id
        self.h.release_server_share(self.tenant, out.vault_id, ok, self.owner)
        with self.assertRaises(VaultStillSealed):
            self.h.release_server_share(self.tenant, out.vault_id, out.items[1].item_id, self.owner)

    def test_share_sealed_for_one_item_cannot_open_as_another(self):
        out = self.create(n=2)
        a, b = self.repo.items[out.vault_id]
        with self.assertRaises(ValueError):
            FakeKms().unseal(a.server_share_sealed,
                             {"tenant_id": str(self.tenant), "vault_id": str(out.vault_id),
                              "item_id": str(b.item_id)})


class LivenessTests(Base):
    def test_checkin_moves_deadline_and_only_owner_may_reset(self):
        out = self.create("liveness", {"interval_days": 30, "grace_days": 0})
        self.seal(out)
        self.clock.advance(days=10)
        d = self.h.record_checkin(self.tenant, out.vault_id, self.owner)
        self.assertEqual((d - self.clock.now()).days, 30)
        with self.assertRaises(NotAuthorized):
            self.h.record_checkin(self.tenant, out.vault_id, self.other)


if __name__ == "__main__":
    unittest.main()
