import { NextResponse, type NextRequest } from "next/server";
import { DEFAULT_LOCALE, isLocale, resolveLocale } from "@/lib/i18n/locales";

const SKIP = /^\/(_next|api|icons|sw\.js|manifest\.json|offline\.html|robots\.txt|sitemap\.xml|favicon)/;

/** Redirect un-prefixed paths to /<locale>/…, choosing by cookie, then Accept-Language. */
export function middleware(req: NextRequest) {
  const { pathname } = req.nextUrl;
  if (SKIP.test(pathname) || /\.[a-z0-9]+$/i.test(pathname)) return NextResponse.next();

  const first = pathname.split("/")[1];
  if (isLocale(first)) return NextResponse.next();

  const cookie = req.cookies.get("vaultla-locale")?.value;
  const locale = isLocale(cookie) ? cookie : resolveLocale(req.headers.get("accept-language")) ?? DEFAULT_LOCALE;
  const url = req.nextUrl.clone();
  url.pathname = `/${locale}${pathname === "/" ? "" : pathname}`;
  const res = NextResponse.redirect(url, 307);        // temporary: depends on the visitor
  res.headers.append("Vary", "Accept-Language, Cookie");
  return res;
}

export const config = { matcher: ["/((?!_next/static|_next/image).*)"] };
