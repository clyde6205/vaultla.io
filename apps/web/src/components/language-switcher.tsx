"use client";
import { usePathname, useRouter } from "next/navigation";
import { LOCALES, isLocale, type Locale } from "@/lib/i18n/locales";

export function LanguageSwitcher({ current }: { current: Locale }) {
  const router = useRouter();
  const pathname = usePathname();
  const onChange = (next: string) => {
    if (!isLocale(next)) return;
    document.cookie = `vaultla-locale=${next}; path=/; max-age=31536000; samesite=lax; secure`;
    const rest = pathname.split("/").slice(2).join("/");
    router.push(`/${next}${rest ? `/${rest}` : ""}`);
  };
  return (
    <label className="inline-flex items-center gap-2 text-sm text-muted-foreground">
      <span className="sr-only">Language</span>
      <select
        value={current}
        onChange={(e) => onChange(e.target.value)}
        className="rounded-md border border-border bg-card px-2 py-1 text-foreground"
      >
        {LOCALES.map((l) => (
          <option key={l.code} value={l.code} lang={l.code}>{l.label}</option>
        ))}
      </select>
    </label>
  );
}
