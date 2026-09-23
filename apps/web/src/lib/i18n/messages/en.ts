// Source of truth. Add keys here first; the catalog test enforces every locale matches.
const en = {
  "hero.title": "Seal it today. Open it in {year}.",
  "hero.sub": "Encrypted on your device. Opens when you decide. Kept for generations.",
  "cta.start": "Create your first capsule",
  "cta.sales": "Talk to sales",
  "value.encrypted": "Files are locked in your browser before upload. We store ciphertext we cannot read.",
  "value.timelock": "Open on a date, in yearly stages, or when check-ins stop. Verified against independent clocks.",
  "value.archive": "Stored in deep archive, built to stay readable for decades.",
  "pricing.title": "Simple pricing, worldwide",
  "plan.free": "Free",
  "plan.premium": "Premium",
  "plan.lifetime": "Lifetime",
  "plan.enterprise": "Enterprise",
  "price.month": "{price} / month",
  "price.year": "{price} / year",
  "price.once": "{price} once",
  "price.from": "From {price} / year",
  "feat.free": "1 GB · fixed-date capsules",
  "feat.premium": "50 GB · every unlock type · guardians",
  "feat.enterprise": "White-label · SSO · audit export · data residency",
  "promise.keep": "If you ever stop paying, your sealed capsules are never deleted.",
  "referral.title": "Invite a friend. You both get {gb} GB.",
  "share.opensIn": "{count, plural, one {Opens in # day} other {Opens in # days}}",
  "vault.count": "{count, plural, one {# capsule} other {# capsules}}",
  "offline.title": "You're offline",
  "offline.body": "Your capsules are safe and encrypted. Reconnect to seal, open or check in.",
  "error.generic": "Something went wrong. Please try again.",
  "checkout.secure": "Secure checkout with local payment methods",
  "tax.note": "Taxes are calculated at checkout",
  "nav.pricing": "Pricing",
  "nav.signin": "Sign in",
} as const;

export type MessageKey = keyof typeof en;
export type Messages = Record<MessageKey, string>;
export default en as Messages;
