import type { Locale } from "./i18n/locales.ts";

/** Currencies with explicit local prices in the price book. Anything else falls back to USD and
 *  Stripe Adaptive Pricing converts at checkout for cards/wallets. */
export const PRICED_CURRENCIES = ["USD", "EUR", "GBP", "CAD", "AUD", "INR", "BRL", "MXN", "PHP", "JPY"] as const;
export type PricedCurrency = (typeof PRICED_CURRENCIES)[number];

const EUROZONE = new Set(["AT","BE","CY","DE","EE","ES","FI","FR","GR","HR","IE","IT","LT","LU","LV","MT","NL","PT","SI","SK"]);
const COUNTRY: Record<string, PricedCurrency> = {
  US: "USD", GB: "GBP", CA: "CAD", AU: "AUD", IN: "INR", BR: "BRL", MX: "MXN", PH: "PHP", JP: "JPY",
};
const LOCALE_DEFAULT: Partial<Record<Locale, PricedCurrency>> = {
  "pt-BR": "BRL", ja: "JPY", hi: "INR", tl: "PHP",
};

/** Country (from a CDN geo header, e.g. cloudfront-viewer-country) wins; else locale hint; else USD. */
export function currencyFor(country: string | null | undefined, locale: Locale): PricedCurrency {
  const c = country?.toUpperCase();
  if (c && EUROZONE.has(c)) return "EUR";
  if (c && COUNTRY[c]) return COUNTRY[c];
  return (!c && LOCALE_DEFAULT[locale]) || "USD";
}
