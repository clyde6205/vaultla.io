import type { MetadataRoute } from "next";
import { LOCALE_CODES } from "@/lib/i18n/locales";

const BASE = process.env.NEXT_PUBLIC_SITE_URL ?? "https://vaultla.io";
const PATHS = ["", "/pricing"];

export default function sitemap(): MetadataRoute.Sitemap {
  return PATHS.flatMap((p) =>
    LOCALE_CODES.map((l) => ({
      url: `${BASE}/${l}${p}`,
      changeFrequency: "monthly" as const,
      alternates: { languages: Object.fromEntries(LOCALE_CODES.map((x) => [x, `${BASE}/${x}${p}`])) },
    })));
}
