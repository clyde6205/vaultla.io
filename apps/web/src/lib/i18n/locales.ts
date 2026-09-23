/** Supported locales. `label` is the endonym shown in the language picker. */
export const LOCALES = [
  { code: "en", label: "English", dir: "ltr" },
  { code: "es", label: "Español", dir: "ltr" },
  { code: "pt-BR", label: "Português (Brasil)", dir: "ltr" },
  { code: "fr", label: "Français", dir: "ltr" },
  { code: "de", label: "Deutsch", dir: "ltr" },
  { code: "it", label: "Italiano", dir: "ltr" },
  { code: "tr", label: "Türkçe", dir: "ltr" },
  { code: "ru", label: "Русский", dir: "ltr" },
  { code: "ar", label: "العربية", dir: "rtl" },
  { code: "hi", label: "हिन्दी", dir: "ltr" },
  { code: "id", label: "Bahasa Indonesia", dir: "ltr" },
  { code: "tl", label: "Filipino", dir: "ltr" },
  { code: "vi", label: "Tiếng Việt", dir: "ltr" },
  { code: "zh-CN", label: "简体中文", dir: "ltr" },
  { code: "ja", label: "日本語", dir: "ltr" },
  { code: "ko", label: "한국어", dir: "ltr" },
] as const;

export type Locale = (typeof LOCALES)[number]["code"];
export const DEFAULT_LOCALE: Locale = "en";
export const LOCALE_CODES: readonly Locale[] = LOCALES.map((l) => l.code);

export const isLocale = (v: string | undefined | null): v is Locale =>
  !!v && (LOCALE_CODES as readonly string[]).includes(v);

export const dirOf = (l: Locale): "ltr" | "rtl" => LOCALES.find((x) => x.code === l)!.dir;

/** Language-only fallbacks for browsers that send a bare or regional tag. */
const ALIASES: Record<string, Locale> = {
  pt: "pt-BR", "pt-pt": "pt-BR", zh: "zh-CN", "zh-hans": "zh-CN", "zh-sg": "zh-CN",
  fil: "tl", "fil-ph": "tl", in: "id", // legacy code for Indonesian
};

/** Pick the best supported locale from an Accept-Language header (q-values honoured). */
export function resolveLocale(acceptLanguage: string | null | undefined): Locale {
  if (!acceptLanguage) return DEFAULT_LOCALE;
  const ranked = acceptLanguage
    .split(",")
    .map((part) => {
      const [tag, ...params] = part.trim().split(";");
      const q = params.map((p) => p.trim()).find((p) => p.startsWith("q="));
      const weight = q ? Number.parseFloat(q.slice(2)) : 1;
      return { tag: tag.trim(), q: Number.isFinite(weight) ? weight : 0 };
    })
    .filter((x) => x.tag && x.tag !== "*" && x.q > 0)
    .sort((a, b) => b.q - a.q);

  for (const { tag } of ranked) {
    const lower = tag.toLowerCase();
    const exact = LOCALE_CODES.find((c) => c.toLowerCase() === lower);
    if (exact) return exact;
    if (ALIASES[lower]) return ALIASES[lower];
    const base = lower.split("-")[0];
    const byBase = LOCALE_CODES.find((c) => c.toLowerCase() === base) ?? ALIASES[base];
    if (byBase) return byBase;
  }
  return DEFAULT_LOCALE;
}
