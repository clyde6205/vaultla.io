"use client";
import { useEffect } from "react";

/** Registers the service worker in production only; dev caching causes confusing stale builds. */
export function RegisterServiceWorker() {
  useEffect(() => {
    if (process.env.NODE_ENV !== "production" || !("serviceWorker" in navigator)) return;
    const onLoad = () =>
      navigator.serviceWorker.register("/sw.js", { scope: "/" }).then((reg) => {
        reg.addEventListener("updatefound", () => {
          const next = reg.installing;
          next?.addEventListener("statechange", () => {
            if (next.state === "installed" && navigator.serviceWorker.controller) next.postMessage("SKIP_WAITING");
          });
        });
      }).catch((err) => console.error("service worker registration failed", err));
    if (document.readyState === "complete") onLoad();
    else window.addEventListener("load", onLoad, { once: true });
    return () => window.removeEventListener("load", onLoad);
  }, []);
  return null;
}
