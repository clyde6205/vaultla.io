/* Vaultla.io service worker.
 *
 * SECURITY RULES (do not relax):
 *   1. NEVER cache /api/*, presigned S3 URLs, or anything carrying Authorization: those
 *      responses contain key shares, download links, or ciphertext. Network-only.
 *   2. Cache only same-origin GET responses for static app assets.
 *   3. Cache names are versioned; old caches are purged on activate.
 * Strategies: precache app shell -> cache-first for hashed static assets ->
 *             stale-while-revalidate for other same-origin GETs -> offline page for navigations.
 */
const VERSION = "v1.0.0";
const SHELL_CACHE = `vaultla-shell-${VERSION}`;
const RUNTIME_CACHE = `vaultla-runtime-${VERSION}`;
const OFFLINE_URL = "/offline.html";   // static file: no redirects, no layout, cacheable
const PRECACHE = [OFFLINE_URL, "/manifest.json", "/icons/icon-192.png", "/icons/icon-512.png"];
const MAX_RUNTIME_ENTRIES = 80;

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keep = new Set([SHELL_CACHE, RUNTIME_CACHE]);
    for (const k of await caches.keys()) if (k.startsWith("vaultla-") && !keep.has(k)) await caches.delete(k);
    if (self.registration.navigationPreload) await self.registration.navigationPreload.enable();
    await self.clients.claim();
  })());
});

self.addEventListener("message", (event) => {
  if (event.data === "SKIP_WAITING") self.skipWaiting();
});

const isSensitive = (req, url) =>
  url.pathname.startsWith("/api/") ||
  req.headers.has("authorization") ||
  url.searchParams.has("X-Amz-Signature") ||
  url.hostname.endsWith(".amazonaws.com");

async function trim(cacheName, max) {
  const cache = await caches.open(cacheName);
  const keys = await cache.keys();
  for (let i = 0; i < keys.length - max; i++) await cache.delete(keys[i]);
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;                       // never touch uploads / mutations
  const url = new URL(req.url);
  if (isSensitive(req, url)) return;                      // network-only, uncached
  if (url.origin !== self.location.origin) return;

  // Navigations: network first (with preload), offline page as fallback.
  if (req.mode === "navigate") {
    event.respondWith((async () => {
      try {
        const preload = await event.preloadResponse;
        return preload || (await fetch(req));
      } catch {
        return (await caches.match(OFFLINE_URL)) || Response.error();
      }
    })());
    return;
  }

  // Immutable build assets: cache-first.
  if (url.pathname.startsWith("/_next/static/") || url.pathname.startsWith("/icons/")) {
    event.respondWith((async () => {
      const cached = await caches.match(req);
      if (cached) return cached;
      const res = await fetch(req);
      if (res.ok) (await caches.open(RUNTIME_CACHE)).put(req, res.clone());
      return res;
    })());
    return;
  }

  // Everything else same-origin: stale-while-revalidate.
  event.respondWith((async () => {
    const cache = await caches.open(RUNTIME_CACHE);
    const cached = await cache.match(req);
    const network = fetch(req).then((res) => {
      if (res.ok && res.type === "basic") { cache.put(req, res.clone()); trim(RUNTIME_CACHE, MAX_RUNTIME_ENTRIES); }
      return res;
    }).catch(() => cached);
    return cached || network;
  })());
});
