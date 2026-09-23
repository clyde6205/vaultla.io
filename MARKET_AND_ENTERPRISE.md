# Vaultla.io — Market, Buyers, Payments (research brief, Sept 23 2026)

Sources are linked inline; claims marked **(inference)** are mine, not from a source. Competitor
claims come from marketing pages and comparison blogs. Treat them as leads to verify, not facts.

## 1. Reality check on "win and own this sector"

Nothing in code can guarantee that; positioning can improve the odds. What the research shows:

* **Event/QR capsules are crowded.** Event Capsule, PicturesQR, Foreverbox, Capsule and Knipsmig all
  sell no-app QR guest uploads with timed reveals ([eventcapsule.camera](https://eventcapsule.camera/about),
  [picturesqr.com](https://www.picturesqr.com/), [foreverbox.app](https://www.foreverbox.app/),
  [getcapsuleapp.com](https://getcapsuleapp.com/wedding-photo-time-capsule)). Competing here on
  features alone is a price war. **(inference)**
* **Legacy planning is fragmented and mostly cheap or free**: Clocr at $59.99/yr, MyWishes free,
  Everplans and GoodTrust as established names ([eternalvault.app comparison](https://eternalvault.app/blog/best-digital-legacy-planning-tools-2026/));
  Afterlife AI at $14.99–$29.99/month ([afterlife.ai](https://www.afterlife.ai/best-digital-legacy-platform)).
* **Institutions already buy this**: SocialArchive pitches digital time capsules to schools and
  universities for anniversaries, with alumni contributions ([socialarchive.com](https://www.socialarchive.com/news/creating-a-digital-time-capsule-for-your-next-school-college-or-university-anniversary)).
* **Trust promises are a live differentiator**: one competitor advertises pay-once sealing and
  early release of capsules if it ever winds down ([time-capsule.io](https://time-capsule.io/)).

**Where Vaultla can be genuinely different** (no source I found claims all of these together; I did
not audit anyone's encryption claims): zero-knowledge encryption in the browser, a format built for a
century, dead-man's-switch and progressive release, white-label B2B at 50,000 capsules, and a
written promise that sealed capsules are never deleted for non-payment (built into `entitlements.py`).

**Recommendation:** win one wedge first, then expand. Best fit for your build: **universities and
institutions** (recurring anniversary/class cycles, big contracts, white-label), fed by the consumer
**event capsule** as the viral top-of-funnel. Legacy/dead-man's-switch is the premium consumer upsell.

## 2. Reasons to buy, by buyer

| Buyer | Reason to buy | Shipped in this repo |
|---|---|---|
| Family / individual | Private by construction; opens on the date you choose; still readable in 100 years | Encryption, triggers, 100-yr format |
| Family / individual | "If I stop paying, nothing is deleted" | `entitlements.py` (tested) |
| Family / individual | Dead man's switch with verified confirmations, not a lapsed email | Liveness trigger + attestations |
| Wedding / event host | Guests scan a QR, no app, no account; gallery stays sealed until the reveal | `event_pages.py` (rules tested; UI not built) |
| Everyone | Global checkout in local currency and local payment methods | `checkout.py`, price book |
| University / alumni office | Provision up to 50,000 capsules by CSV, own branding, storage dashboards | `provisioning.py` (tested); dashboard UI not built |
| Corporation | SSO, audit export, data residency, invoicing with PO and net terms | Schema + entitlements + invoice builder; SSO/SCIM to buy |
| Everyone | Viral: invite a friend, both get storage; shareable countdown teaser | `referrals.py`, `teaser.py` (tested) |

## 3. What corporations require before they sign

Consistent across the enterprise-readiness sources: SAML/OIDC SSO, SCIM provisioning, exportable
audit logs, RBAC, encryption in transit and at rest, and a SOC 2 Type II report as the usual gate
above a certain deal size ([startwithidentity.com](https://startwithidentity.com/articles/b2b-saas-security-tools-enterprise-procurement/),
[ssojet.com](https://ssojet.com/blog/enterprise-identity-management-for-saas)). European and global
buyers also ask for GDPR alignment, data residency and often ISO 27001. Buyers expect a DPA, a
documented deletion process, and a data export on exit
([ssojet checklist](https://dev.to/ssojet/enterprise-identity-management-checklist-for-saas-founders-3me3)).

Build order that the sources recommend, and that this repo follows: **RBAC → audit log → SSO
(bought, not built) → SCIM when a customer asks in writing**
([hashorn.com](https://hashorn.com/blog/enterprise-ready-saas-sso-scim-audit-logs)).

Status here: tenant isolation, audit hash-chain, data-region and legal-hold columns, SSO
connection table and enterprise entitlements exist. **Not done:** RBAC enforcement in the API,
SSO/SCIM integration, audit-export endpoint, any certification. SOC 2 Type II needs an observation
window of months plus an auditor. No amount of code shortens that. Start the process (policies,
evidence tooling) before the first enterprise conversation.

## 4. Global payments: decision you need to make

**What the code does (Stripe path):** Checkout with `automatic_tax`, `tax_id_collection` (B2B
reverse charge), `adaptive_pricing` and *no* `payment_method_types`. Setting that parameter forces
card-only and silently disables Adaptive Pricing and local methods such as iDEAL
([Studio2C guide](https://www.studio2c.ee/the-complete-guide-to-setting-up-stripe-tax-adaptive-pricing-and-invoicing-for-european-indie-saas-founders/)).
Adaptive Pricing shows local currency in 150+ countries; buyers, not you, pay a 2–4% conversion
margin, and tax is computed on the base USD price
([Stripe docs](https://docs.stripe.com/tax/calculating/adaptive-pricing),
[Stripe support](https://support.stripe.com/questions/adaptive-pricing)). For subscriptions it
supports only methods that work in both currencies (cards, Apple Pay, Link)
([Stripe support](https://support.stripe.com/questions/adaptive-pricing-for-subscriptions)). We set
explicit local prices for 10 currencies; Stripe does not convert those, and converts the rest.
Enterprise is invoiced with net terms, not card checkout.

**Two ways to run it. This choice is yours, and it matters more than any code:**

| | Stripe direct (built) | Merchant of Record (Paddle / Lemon Squeezy) (not built) |
|---|---|---|
| Who owes VAT/GST | **You** register, file, remit. Stripe Tax calculates only | The MoR files and remits |
| Cost | ~2.9% + 30¢ plus Tax and FX; sources report real global cost often 5%+ | Around 5% + 50¢ all-in ([globalsolo](https://www.globalsolo.global/blog/stripe-vs-paddle-vs-lemon-squeezy-2026)) |
| Enterprise (PO, net-30) | Strong | Paddle yes; Lemon Squeezy reported weak, with a practical B2B ceiling around $500k–1M ARR ([fintechspecs](https://fintechspecs.com/blog/stripe-vs-paddle-vs-lemon-squeezy-vs-polar-merchant-of-record-b2b-saas/)) |

**(inference)** For a small team selling to consumers in 100+ countries, a Merchant of Record removes
the largest legal risk (global sales tax). Stripe direct wins for enterprise invoicing. A common
split is MoR for consumers and Stripe for enterprise. The schema has a `provider` column ready
for this; **only the Stripe adapter is written.** Ask a cross-border tax adviser before choosing.

**Your account constraints (worth checking before anything else):**

* A Stripe **US account registered as an individual/sole proprietor requires an active US phone
  number for the representative.** If the company was formed through **Stripe Atlas**, the phone
  number **can be in any country** ([Stripe support](https://support.stripe.com/questions/phone-number-requirements-for-us-stripe-accounts)).
  Given that you have no US number, an Atlas-formed (or other US-formed) company looks like the
  route to verify, rather than an individual account. Third-party guides describe an ordered path:
  LLC, then EIN, then US bank account, then Stripe application
  ([rocketwave.co](https://rocketwave.co/stripe-setup-non-us-resident-us-llc/)).
* You bank only with NFCU. **I don't know whether NFCU will open or accept payouts to a business
  account for a new LLC;** confirm with them before forming anything. Stripe also verifies entity,
  EIN and address ([Stripe support](https://support.stripe.com/questions/requirements-for-having-a-us-stripe-account)).
* Not legal or tax advice. Have an accountant confirm the structure, especially U.S. tax
  treatment for a citizen living in the Philippines.

## 5. Lifetime plan economics ($149 for 50 GB)

Deep Archive is $0.00099 per GB-month, with a 180-day minimum, and $0.0025/GB for bulk restore
(up to 48 h); egress is ~$0.09/GB ([usage.ai](https://www.usage.ai/blogs/aws/storage-cost/glacier-deep-archive-pricing/),
[AWS announcement](https://aws.amazon.com/about-aws/whats-new/2019/03/S3-glacier-deep-archive/)).
Verify current prices; they vary by region and change.

Rough math at today's rates, 50 GB fully used: storage ≈ $0.05/month ≈ **$59 over 100 years,
nominal**; one full restore+download ≈ $0.13 + $4.50. So raw storage is survivable **(inference)**.
The real liabilities are payment fees, support, compliance, engineering, and *the company
outliving its promise*. Options: cap lifetime quotas, put a share of each lifetime sale into a
reserve, and publish a wind-down/escrow policy (a competitor already advertises one).

## 6. Viral and utility elements

Built and tested (logic only): referral storage bonus with anti-farming rules (reward only after
verified email and a sealed vault, caps, velocity holds); shareable teaser countdown that leaks
nothing; QR event entry with hashed tokens and abuse limits; CSV bulk provisioning with formula
injection defence. **Not built (UI or logic):** gift capsules, embeddable countdown widget, social
share cards, yearly "open your capsule" reminder emails, guest hybrid-encryption flow, native apps.

## 7. Languages

16 locales (en, es, pt-BR, fr, de, it, tr, ru, ar, hi, id, tl, vi, zh-CN, ja, ko) with correct
plural rules (incl. Russian and Arabic), RTL for Arabic, hreflang and localized sitemap, per-currency
money formatting. **All non-English text is my draft.** Have a native speaker review each catalog
before launch, and never ship legal, tax or pricing-policy text on machine translation alone.
