import type { MetadataRoute } from "next";
export default function robots(): MetadataRoute.Robots {
  return { rules: [{ userAgent: "*", allow: "/", disallow: ["/api/", "/*/e/", "/*/capsule/"] }],
           sitemap: `${process.env.NEXT_PUBLIC_SITE_URL ?? "https://vaultla.io"}/sitemap.xml` };
}
