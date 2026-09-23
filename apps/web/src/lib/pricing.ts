import book from "./price-book.json";
import type { PricedCurrency } from "./currency.ts";

type PlanKey = keyof typeof book.plans;
/** Minor units for a plan in a currency (generated from the backend catalog: single source of truth). */
export const priceMinor = (plan: PlanKey, cur: PricedCurrency): number =>
  (book.plans[plan] as Record<string, number>)[cur] ?? book.plans[plan].USD;
export const enterpriseFromMinor = book.enterprise_min_annual_usd_minor;
