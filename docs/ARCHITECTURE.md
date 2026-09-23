# Vaultla.io — Architecture

## 1. Repository layout

Legend: ✅ built in this scaffold · 🧪 built and unit-tested · ⬜ planned (not yet written)

```
vaultla/
├── README.md                                  ✅
├── docs/ARCHITECTURE.md                       ✅  (this file)
├── docs/MARKET_AND_ENTERPRISE.md              ✅  research: competitors, corporate buyers, payments
├── db/migrations/
│   ├── 0001_init.sql                          ✅  schema, RLS, roles, indexes, audit hash-chain
│   └── 0002_billing_growth_enterprise.sql     ✅  billing, referrals, teasers, SSO refs, data region
├── apps/web/                                  Next.js 15 PWA (React 19, TypeScript, Tailwind, shadcn/ui)
│   ├── public/
│   │   ├── manifest.json                      ✅
│   │   ├── sw.js                              ✅  service worker (never caches API/S3/keys)
│   │   └── icons/                             ✅
│   ├── src/app/
│   │   ├── [locale]/layout.tsx, page.tsx      ✅  localized landing (16 languages, RTL, hreflang)
│   │   ├── [locale]/pricing/page.tsx          ✅  geo-currency pricing, never-delete promise
│   │   ├── sitemap.ts, robots.ts, globals.css ✅
│   │   ├── (checkout success, dashboard)…     ⬜
│   │   ├── (auth)/…                           ⬜  sign-in, passkey enrolment
│   │   ├── vaults/new, vaults/[id]/…          ⬜  create / seal / open flows (use lib/crypto)
│   │   ├── checkin/                           ⬜  liveness check-in
│   │   ├── e/[slug]/                          ⬜  public event page (guest upload)            [Module C]
│   │   └── admin/…                            ⬜  white-label dashboard, CSV provisioning      [Module C]
│   ├── public/offline.html                    ✅  static, multilingual offline page
│   ├── src/middleware.ts                      ✅  locale routing
│   ├── src/lib/i18n/                          🧪  16 catalogs, ICU-lite plurals, tested
│   ├── src/lib/{currency,pricing}.ts          ✅  price-book.json is generated from the backend
│   ├── src/lib/crypto/
│   │   ├── shamir.ts                          🧪  GF(256) secret sharing
│   │   └── vault-crypto.ts                    🧪  AES-256-GCM chunked, key wrap, KDFs
│   ├── src/components/register-sw.tsx         ✅
│   ├── src/components/ui/                     ⬜  shadcn components (add with `npx shadcn add`)
│   └── tests/vault-crypto.test.ts             🧪
├── services/api/                              Python 3.12 · FastAPI · Lambda (Mangum) or ECS Fargate
│   ├── app/
│   │   ├── main.py                            ✅  app, error handlers, security headers
│   │   ├── core/{config,errors}.py            ✅
│   │   ├── chronos/                           ── Module B ──
│   │   │   ├── time_source.py                 🧪  NTP quorum (median, spread, anti-spoof)
│   │   │   ├── triggers.py                    🧪  fixed date · progressive · liveness (pure logic)
│   │   │   ├── scanner.py                     🧪  daily sweep, fail-closed, idempotent
│   │   │   └── lambda_handler.py              ✅  EventBridge entrypoint
│   │   ├── vault/                             ── Module A ──
│   │   │   ├── ports.py                       ✅  interfaces (store, KMS, repository)
│   │   │   └── handler.py                     🧪  create / seal / check-in / key release
│   │   ├── adapters/
│   │   │   ├── aws.py                         ✅  S3 + KMS (needs integration test)
│   │   │   ├── postgres_vault.py              ✅  tenant-scoped repo (needs integration test)
│   │   │   └── postgres_chronos.py            ✅  restricted-role repo (needs integration test)
│   │   ├── api/{auth,schemas,vaults}.py       ✅  JWT → tenant, strict request models, routes
│   │   ├── billing/                           ── Module D ──
│   │   │   ├── catalog.py, entitlements.py    🧪  price book (10 currencies), tiers, never-delete rule
│   │   │   ├── checkout.py                    🧪  Stripe param builders (tax, adaptive pricing, invoices)
│   │   │   └── webhooks.py                    🧪  signature verify, idempotent handling
│   │   ├── growth/                            ── Module C (logic) + viral ──
│   │   │   ├── referrals.py, teaser.py        🧪
│   │   │   ├── event_pages.py                 🧪  QR entry rules
│   │   │   └── provisioning.py                🧪  50,000-row CSV validation
│   │   └── tenants/… (branding upload, dashboard API, event page API)   ⬜
│   ├── tests/                                 🧪  89 unit tests, standard library only
│   ├── scripts/                               ✅  stripe_setup_prices.py, export_price_book.py
│   ├── pyproject.toml, Dockerfile             ✅
├── infra/terraform/
│   ├── storage.tf                             ✅  KMS, S3, lifecycle → Deep Archive, TLS-only, CORS
│   └── chronos.tf                             ✅  Lambda, EventBridge Scheduler, DLQ + alarm
└── .github/workflows/                         ⬜  CI: tests, mypy, ruff, bandit, pip-audit, tfsec
```

## 2. How the zero-knowledge design works

The browser generates a random 256-bit key (DEK) per item, encrypts with AES-256-GCM in 4 MiB
authenticated chunks, then splits the DEK with Shamir 2-of-3:

| Share | Lives with | Released when |
|---|---|---|
| 1 | Server, sealed under KMS (context-bound to tenant/vault/item) | Chronos trigger has matured, caller is entitled |
| 2 | Owner, wrapped by a key from passphrase or passkey (PRF) | Owner's device unwraps it |
| 3 | Guardian / beneficiary, wrapped by a recovery code | Holder enters recovery code |

One share reveals nothing. Server alone: cannot decrypt. Owner alone: cannot open early.
After maturity: server share + either holder share → key, in the browser.

## 3. Honest limits (read before relying on this for anything critical)

1. **The time-lock is policy, not mathematics.** Early release requires subverting both Chronos
   and the KMS-sealed share, but it is not impossible. For a cryptographic guarantee, layer
   drand/tlock (threshold time-lock encryption) as a second wrapper on the server share.
2. **Two holders can open early.** Owner share + guardian share = 2-of-3 without the server.
   That is deliberate emergency access; use a different split if you need "nobody can open early".
3. **Loss of both client shares = permanent loss.** That is the price of zero-knowledge.
4. **Deep Archive** costs little but not zero, has a 180-day minimum, and needs 12–48 h to restore.
5. **PBKDF2** is used for passphrases because Web Crypto lacks Argon2id. Prefer passkeys (PRF)
   or add an Argon2id WASM build.
6. **Long retention (5-20 years is the target; the engine allows far longer)** needs process as much as code: format versioning (`envelope_version`,
   ciphertext header) is in place; periodic re-wrapping/migration drills and post-quantum
   (ML-KEM) wrapping are future work.
7. **Nothing here is certified.** "Financial/military grade" is a property of audits, key
   ceremonies, and operations, not of a repository. Budget for an independent cryptography
   review and penetration test before handling real user data.

## 4. Tenant isolation

Every tenant-owned table has `tenant_id`, `FORCE ROW LEVEL SECURITY`, and a policy comparing to
`current_setting('app.tenant_id')`, set per transaction from the verified JWT. Unset ⇒ zero rows.
Composite foreign keys `(tenant_id, id)` make cross-tenant references impossible at the schema level.
Consumers each get a `personal` tenant; universities/brands are `enterprise` tenants. The Chronos
scanner uses a separate DB role that can read sealed vaults across tenants but may only update
scheduling columns.
