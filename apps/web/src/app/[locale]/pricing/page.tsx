import { headers } from "next/headers";
import { notFound } from "next/navigation";
import { LanguageSwitcher } from "@/components/language-switcher";
import { currencyFor } from "@/lib/currency";
import { formatMoney, getTranslator, isLocale, type Locale } from "@/lib/i18n";
import { enterpriseFromMinor, priceMinor } from "@/lib/pricing";

export const metadata = { title: "Pricing" };

export default async function Pricing({ params }: { params: Promise<{ locale: string }> }) {
  const { locale } = await params;
  if (!isLocale(locale)) notFound();
  const l: Locale = locale;
  const t = await getTranslator(l);
  const h = await headers();
  // CloudFront / Vercel geo headers. Absent in dev: falls back to the locale's usual currency.
  const country = h.get("cloudfront-viewer-country") ?? h.get("x-vercel-ip-country");
  const cur = currencyFor(country, l);
  const money = (minor: number, c = cur) => formatMoney(minor, c, l);

  const plans = [
    { id: "free", name: t("plan.free"), price: money(0), sub: null, feat: t("feat.free"), accent: false },
    { id: "premium", name: t("plan.premium"), price: t("price.month", { price: money(priceMinor("premium_monthly", cur)) }),
      sub: t("price.year", { price: money(priceMinor("premium_yearly", cur)) }), feat: t("feat.premium"), accent: true },
    { id: "lifetime", name: t("plan.lifetime"), price: t("price.once", { price: money(priceMinor("lifetime", cur)) }),
      sub: null, feat: t("feat.premium"), accent: false },
    { id: "enterprise", name: t("plan.enterprise"), price: t("price.from", { price: money(enterpriseFromMinor, "USD") }),
      sub: null, feat: t("feat.enterprise"), accent: false },
  ];

  return (
    <main className="mx-auto max-w-5xl px-6 py-10">
      <div className="mb-10 flex items-center justify-between">
        <h1 className="font-display text-4xl font-semibold">{t("pricing.title")}</h1>
        <LanguageSwitcher current={l} />
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {plans.map((p) => (
          <section key={p.id} id={p.id} className={`flex flex-col gap-3 rounded-lg border p-5 ${p.accent ? "border-primary" : "border-border"} bg-card`}>
            <h2 className="font-display text-xl font-semibold">{p.name}</h2>
            <p className="text-lg">{p.price}</p>
            {p.sub && <p className="text-sm text-muted-foreground">{p.sub}</p>}
            <p className="mt-auto text-sm text-muted-foreground">{p.feat}</p>
          </section>
        ))}
      </div>

      <div className="mt-8 space-y-2 text-sm text-muted-foreground">
        <p className="text-foreground">{t("promise.keep")}</p>
        <p>{t("checkout.secure")}</p>
        <p>{t("tax.note")}</p>
        <p>{t("referral.title", { gb: 1 })}</p>
      </div>
    </main>
  );
}
