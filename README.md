# Vaultla.io

Zero-knowledge digital time capsules and legacy vaults. PWA (Next.js) + FastAPI on AWS.

**Status: foundation scaffold.** Modules A (vault handler) and B (Chronos time-lock) are implemented and tested. Module D
(billing) and the logic layer of Module C (CSV provisioning, QR event rules, referrals, teasers)
are implemented and tested; Module C's dashboards and event-page UI are not built. Start with
`docs/MARKET_AND_ENTERPRISE.md`. See `docs/ARCHITECTURE.md`
for the full layout, what is tested, and the honest security limits.

## What has been verified (in the build sandbox, no network)

| Area | Result |
|---|---|
| Python core: triggers, NTP quorum, scanner, vault handler, billing, referrals, provisioning | 89 unit tests pass |
| Browser crypto and i18n (16 locales: keys, placeholders, plurals, RTL, currency) | 15 tests pass (Node 22) |
| Not run yet | SQL migrations on real Postgres · Terraform · AWS/Stripe/Postgres adapters · Next.js build and pages · FastAPI app |

The "not run yet" items were written carefully but have never executed. Treat them as untested.

## Run the tests

```bash
# Python (standard library only)
cd services/api && python3 -m unittest discover -s tests -t .

# Browser crypto (Node >= 22.18)
cd apps/web && node --test tests/vault-crypto.test.ts tests/i18n.test.ts
```

## First real steps

1. `cd apps/web && npm install && npm run build`, and fix whatever the first build reports.
2. Create a Postgres 15 database and apply `db/migrations/0001_init.sql`; then set passwords for
   the `vaultla_app` and `vaultla_chronos` roles.
3. `cd services/api && pip install -e ".[dev]"`, then run mypy/ruff and integration-test the adapters.
4. Review `infra/terraform/*.tf` with `terraform validate` and `tfsec` before applying anywhere.
