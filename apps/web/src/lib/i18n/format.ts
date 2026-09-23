/**
 * ICU-lite message formatting: `{name}` interpolation and one level of
 * `{count, plural, zero {..} one {..} two {..} few {..} many {..} other {..}}` with `#` for the
 * number. Plural category selection uses Intl.PluralRules, so Russian/Arabic rules are correct.
 * Values are inserted as plain text; escaping is the renderer's job (React escapes by default).
 */
import type { Locale } from "./locales.ts";

type Values = Record<string, string | number>;

function findClose(s: string, open: number): number {
  let depth = 0;
  for (let i = open; i < s.length; i++) {
    if (s[i] === "{") depth++;
    else if (s[i] === "}" && --depth === 0) return i;
  }
  throw new Error(`Unbalanced braces in message: ${s}`);
}

export function format(message: string, values: Values, locale: Locale): string {
  let out = "";
  for (let i = 0; i < message.length; i++) {
    const ch = message[i];
    if (ch !== "{") { out += ch; continue; }
    const end = findClose(message, i);
    const inner = message.slice(i + 1, end);
    const m = /^\s*(\w+)\s*,\s*plural\s*,([\s\S]*)$/.exec(inner);
    if (m) {
      const n = Number(values[m[1]]);
      if (!Number.isFinite(n)) throw new Error(`Missing numeric value for "${m[1]}"`);
      const branches: Record<string, string> = {};
      const re = /(\w+)\s*\{/g;
      let bm: RegExpExecArray | null;
      const body = m[2];
      while ((bm = re.exec(body))) {
        const open = bm.index + bm[0].length - 1;
        const close = findClose(body, open);
        branches[bm[1]] = body.slice(open + 1, close);
        re.lastIndex = close + 1;
      }
      const cat = new Intl.PluralRules(locale).select(n);
      const chosen = branches[cat] ?? branches.other;
      if (chosen === undefined) throw new Error(`Plural needs an "other" branch: ${message}`);
      out += chosen.replace(/#/g, new Intl.NumberFormat(locale).format(n));
    } else {
      const key = inner.trim();
      if (!(key in values)) throw new Error(`Missing value for "{${key}}"`);
      out += String(values[key]);
    }
    i = end;
  }
  return out;
}

/** Names of all `{placeholders}` (including plural variables) used by a message. */
export function placeholders(message: string): string[] {
  const names = new Set<string>();
  for (let i = 0; i < message.length; i++) {
    if (message[i] !== "{") continue;
    const end = findClose(message, i);
    const inner = message.slice(i + 1, end);
    const m = /^\s*(\w+)\s*,\s*plural\s*,/.exec(inner);
    if (m) {
      names.add(m[1]);
      // placeholders inside branches
      for (const sub of inner.matchAll(/\{([^{}]*)\}/g)) if (/^\s*\w+\s*$/.test(sub[1])) names.add(sub[1].trim());
    } else names.add(inner.trim());
    i = end;
  }
  return [...names].sort();
}

/** Money from MINOR units (cents) using the currency's own decimal rules (JPY has none). */
export function formatMoney(minor: number, currency: string, locale: Locale): string {
  const nf = new Intl.NumberFormat(locale, { style: "currency", currency });
  const digits = nf.resolvedOptions().maximumFractionDigits ?? 2;
  return nf.format(minor / 10 ** digits);
}

export const formatDate = (d: Date | number, locale: Locale, opts?: Intl.DateTimeFormatOptions): string =>
  new Intl.DateTimeFormat(locale, opts ?? { dateStyle: "long", timeZone: "UTC" }).format(d);
