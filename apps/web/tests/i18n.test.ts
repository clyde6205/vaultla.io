import assert from "node:assert/strict";
import { test } from "node:test";
import { LOCALE_CODES, dirOf, format, formatMoney, getTranslator, placeholders, resolveLocale, type MessageKey } from "../src/lib/i18n/index.ts";
import en from "../src/lib/i18n/messages/en.ts";

const load = async (l: string) => (await import(`../src/lib/i18n/messages/${l}.ts`)).default as Record<string, string>;

test("every locale has exactly the English keys, no blanks, and identical placeholders", async () => {
  const keys = Object.keys(en) as MessageKey[];
  for (const loc of LOCALE_CODES) {
    const m = await load(loc);
    assert.deepEqual(Object.keys(m).sort(), [...keys].sort(), `${loc}: key mismatch`);
    for (const k of keys) {
      assert.ok(m[k].trim().length > 0, `${loc}.${k} is blank`);
      assert.deepEqual(placeholders(m[k]), placeholders(en[k]), `${loc}.${k}: placeholders differ from English`);
    }
  }
});

test("every message formats without throwing in its own locale", async () => {
  const vals = { year: 2126, price: "$4.99", gb: 1, count: 3 };
  for (const loc of LOCALE_CODES) {
    const t = await getTranslator(loc);
    for (const k of Object.keys(en) as MessageKey[]) {
      const s = t(k, vals);
      assert.ok(s.length > 0 && !/[{}]/.test(s), `${loc}.${k} -> ${s}`);
    }
  }
});

test("plural rules: English, Russian, Arabic", () => {
  const ru = "{count, plural, one {# день} few {# дня} many {# дней} other {# дня}}";
  assert.equal(format(ru, { count: 1 }, "ru"), "1 день");
  assert.equal(format(ru, { count: 3 }, "ru"), "3 дня");
  assert.equal(format(ru, { count: 5 }, "ru"), "5 дней");
  assert.equal(format(ru, { count: 21 }, "ru"), "21 день");
  const ar = "{count, plural, zero {صفر} one {واحد} two {اثنان} few {قليل} many {كثير} other {آخر}}";
  assert.deepEqual([0, 1, 2, 5, 11, 100].map((n) => format(ar, { count: n }, "ar")), ["صفر", "واحد", "اثنان", "قليل", "كثير", "آخر"]);
  const en1 = "{count, plural, one {# capsule} other {# capsules}}";
  assert.equal(format(en1, { count: 1 }, "en"), "1 capsule");
  assert.equal(format(en1, { count: 1234 }, "en"), "1,234 capsules");
});

test("missing values fall back to English rather than crashing", async () => {
  const t = await getTranslator("es");
  const out = t("hero.title");                          // {year} not supplied: must not throw
  assert.ok(out.length > 0);
  assert.equal(t("hero.title", { year: 2126 }), "Séllalo hoy. Ábrelo en 2126.");
});

test("Accept-Language resolution", () => {
  assert.equal(resolveLocale("es-MX,es;q=0.9,en;q=0.8"), "es");
  assert.equal(resolveLocale("pt-PT"), "pt-BR");
  assert.equal(resolveLocale("zh-TW;q=0.5, ja;q=0.9"), "ja");
  assert.equal(resolveLocale("fil-PH"), "tl");
  assert.equal(resolveLocale("zh"), "zh-CN");
  assert.equal(resolveLocale("xx, *;q=0.1"), "en");
  assert.equal(resolveLocale(null), "en");
  assert.equal(resolveLocale("fr;q=0, de;q=0.4"), "de");
});

test("RTL only for Arabic", () => {
  assert.deepEqual(LOCALE_CODES.filter((l) => dirOf(l) === "rtl"), ["ar"]);
});

test("money uses each currency's own decimals", () => {
  assert.match(formatMoney(499, "USD", "en"), /\$4\.99/);
  assert.match(formatMoney(690, "JPY", "ja"), /690/);
  assert.match(formatMoney(19900, "INR", "hi"), /199/);
});
