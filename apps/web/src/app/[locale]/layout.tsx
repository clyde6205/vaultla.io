import type { Metadata, Viewport } from "next";
import { Barlow, Barlow_Semi_Condensed } from "next/font/google";
import { notFound } from "next/navigation";
import { RegisterServiceWorker } from "@/components/register-sw";
import { LOCALE_CODES, dirOf, isLocale } from "@/lib/i18n/locales";
import "../globals.css";

const body = Barlow({ subsets: ["latin", "latin-ext", "vietnamese"], weight: ["400", "500", "600"], variable: "--font-body", display: "swap" });
const display = Barlow_Semi_Condensed({ subsets: ["latin", "latin-ext", "vietnamese"], weight: ["500", "600", "700"], variable: "--font-display", display: "swap" });

export const generateStaticParams = () => LOCALE_CODES.map((locale) => ({ locale }));

export async function generateMetadata({ params }: { params: Promise<{ locale: string }> }): Promise<Metadata> {
  const { locale } = await params;
  const base = process.env.NEXT_PUBLIC_SITE_URL ?? "https://vaultla.io";
  return {
    metadataBase: new URL(base),
    title: { default: "Vaultla.io", template: "%s · Vaultla.io" },
    manifest: "/manifest.json",
    applicationName: "Vaultla.io",
    appleWebApp: { capable: true, title: "Vaultla", statusBarStyle: "black-translucent" },
    icons: { icon: "/icons/icon-192.png", apple: "/icons/icon-192.png" },
    // hreflang: tells search engines every localized twin of this page.
    alternates: {
      canonical: `/${locale}`,
      languages: { ...Object.fromEntries(LOCALE_CODES.map((l) => [l, `/${l}`])), "x-default": "/en" },
    },
  };
}

export const viewport: Viewport = { themeColor: "#0a0b0d", width: "device-width", initialScale: 1, viewportFit: "cover" };

export default async function RootLayout({ children, params }: { children: React.ReactNode; params: Promise<{ locale: string }> }) {
  const { locale } = await params;
  if (!isLocale(locale)) notFound();
  return (
    <html lang={locale} dir={dirOf(locale)} className={`dark ${body.variable} ${display.variable}`}>
      <body>
        {children}
        <RegisterServiceWorker />
      </body>
    </html>
  );
}
