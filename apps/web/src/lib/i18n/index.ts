import { format } from "./format.ts";
import { DEFAULT_LOCALE, type Locale } from "./locales.ts";
import type { MessageKey, Messages } from "./messages/en.ts";

export * from "./locales.ts";
export { format, formatDate, formatMoney, placeholders } from "./format.ts";
export type { MessageKey, Messages } from "./messages/en.ts";

/** Lazy loaders: only the active locale's catalog ships to the browser. */
const loaders: Record<Locale, () => Promise<{ default: Messages }>> = {
  en: () => import("./messages/en.ts"), es: () => import("./messages/es.ts"),
  "pt-BR": () => import("./messages/pt-BR.ts"), fr: () => import("./messages/fr.ts"),
  de: () => import("./messages/de.ts"), it: () => import("./messages/it.ts"),
  tr: () => import("./messages/tr.ts"), ru: () => import("./messages/ru.ts"),
  ar: () => import("./messages/ar.ts"), hi: () => import("./messages/hi.ts"),
  id: () => import("./messages/id.ts"), tl: () => import("./messages/tl.ts"),
  vi: () => import("./messages/vi.ts"), "zh-CN": () => import("./messages/zh-CN.ts"),
  ja: () => import("./messages/ja.ts"), ko: () => import("./messages/ko.ts"),
};

export type Translator = (key: MessageKey, values?: Record<string, string | number>) => string;

export async function getTranslator(locale: Locale): Promise<Translator> {
  const [primary, fallback] = await Promise.all([
    loaders[locale](),
    locale === DEFAULT_LOCALE ? null : loaders[DEFAULT_LOCALE](),
  ]);
  return (key, values = {}) => {
    const msg = primary.default[key] ?? fallback?.default[key] ?? key;   // never render blank
    try {
      return format(msg, values, locale);
    } catch {
      // Bad/missing values or a broken translation: degrade to English, then to the raw text.
      // Never throw from the UI layer for a copy problem.
      try {
        return fallback ? format(fallback.default[key], values, DEFAULT_LOCALE) : msg;
      } catch {
        return fallback?.default[key] ?? msg;
      }
    }
  };
}
