import Link from "next/link";
import { LanguageSwitcher } from "@/components/language-switcher";
import { getTranslator, isLocale, type Locale } from "@/lib/i18n";
import { notFound } from "next/navigation";

const YEARS = 20;   // most capsules open in 5-20 years; the engine supports far longer

function YearsRuler() {
  const ticks = Array.from({ length: YEARS + 1 }, (_, i) => i);
  return (
    <svg viewBox="0 0 1000 64" aria-hidden="true" className="w-full rtl:-scale-x-100">
      {ticks.map((i) => {
        const x = 4 + (i * 992) / YEARS;
        const major = i % 5 === 0;
        return <line key={i} x1={x} x2={x} y1={major ? 14 : 30} y2={48} stroke="hsl(220 9% 30%)" strokeWidth={major ? 1.5 : 1} />;
      })}
      <line x1={4} x2={996} y1={48} y2={48} stroke="hsl(220 9% 30%)" strokeWidth={1.5} />
      <circle cx={4} cy={48} r={5} fill="hsl(39 58% 60%)" />
      <circle cx={996} cy={48} r={5} fill="none" stroke="hsl(39 58% 60%)" strokeWidth={1.5} />
    </svg>
  );
}

export default async function Home({ params }: { params: Promise<{ locale: string }> }) {
  const { locale } = await params;
  if (!isLocale(locale)) notFound();
  const l: Locale = locale;
  const t = await getTranslator(l);
  const openYear = new Date().getFullYear() + YEARS;

  return (
    <main className="mx-auto flex min-h-dvh max-w-4xl flex-col gap-10 px-6 py-8">
      <nav className="flex items-center justify-between">
        <span className="font-display text-lg font-semibold">Vaultla.io</span>
        <div className="flex items-center gap-5">
          <Link href={`/${l}/pricing`} className="text-sm text-muted-foreground hover:text-foreground">{t("nav.pricing")}</Link>
          <LanguageSwitcher current={l} />
        </div>
      </nav>

      <section className="flex flex-1 flex-col justify-center gap-10">
        <h1 className="font-display text-5xl font-semibold leading-[1.05] tracking-tight sm:text-7xl">
          {t("hero.title", { year: openYear })}
        </h1>
        <YearsRuler />
        <p className="max-w-xl text-lg text-muted-foreground">{t("hero.sub")}</p>
        <div className="flex flex-wrap gap-3">
          <Link href={`/${l}/pricing`} className="rounded-md bg-primary px-5 py-3 font-medium text-primary-foreground">{t("cta.start")}</Link>
          <Link href={`/${l}/pricing#enterprise`} className="rounded-md border border-border px-5 py-3 font-medium">{t("cta.sales")}</Link>
        </div>
      </section>

      <section className="grid gap-8 text-[15px] leading-relaxed text-muted-foreground sm:grid-cols-3">
        <p>{t("value.encrypted")}</p>
        <p>{t("value.timelock")}</p>
        <p>{t("value.archive")}</p>
      </section>

      <p className="border-t border-border pt-6 text-sm text-muted-foreground">{t("promise.keep")}</p>
    </main>
  );
}
