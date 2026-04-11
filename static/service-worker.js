/*
    Service worker for offline-first behavior.

    Responsibilities:
    - Precache core shell assets (home, CSS, offline page, manifest).
    - Serve cached responses first, then network.
    - Cache successful same-origin GET responses on demand.
    - Return /static/offline.html when navigation requests fail offline.
*/

const CACHE_NAME = "shelah-cache-v2";
const CORE_ASSETS = [
    "/static/style.css",
    "/static/offline.html",
    "/manifest.webmanifest"
];

self.addEventListener("install", (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => cache.addAll(CORE_ASSETS))
    );
    self.skipWaiting();
});

self.addEventListener("activate", (event) => {
    event.waitUntil(
        caches.keys().then((keys) => {
            return Promise.all(
                keys
                    .filter((key) => key !== CACHE_NAME)
                    .map((key) => caches.delete(key))
            );
        })
    );
    self.clients.claim();
});

self.addEventListener("fetch", (event) => {
    if (event.request.method !== "GET") {
        return;
    }

    const requestUrl = new URL(event.request.url);
    const isSameOrigin = requestUrl.origin === self.location.origin;
    const isApiRequest = isSameOrigin && requestUrl.pathname.startsWith("/api/");
    const isNavigation = event.request.mode === "navigate";

    // Always fetch live API data so zmanim/holidays/preferences don't get stale.
    if (isApiRequest) {
        event.respondWith(
            fetch(event.request).catch(() => {
                return new Response(JSON.stringify({ error: "Offline" }), {
                    status: 503,
                    statusText: "Offline",
                    headers: { "Content-Type": "application/json" }
                });
            })
        );
        return;
    }

    // Prefer live HTML for navigation, with offline fallback when unavailable.
    if (isNavigation) {
        event.respondWith(
            fetch(event.request)
                .then((response) => {
                    if (response.ok && isSameOrigin) {
                        const cloned = response.clone();
                        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, cloned));
                    }
                    return response;
                })
                .catch(() => {
                    return caches.match(event.request).then((cached) => {
                        return cached || caches.match("/static/offline.html");
                    });
                })
        );
        return;
    }

    event.respondWith(
        caches.match(event.request).then((cached) => {
            if (cached) {
                return cached;
            }

            return fetch(event.request)
                .then((response) => {
                    const cloned = response.clone();
                    if (response.ok && isSameOrigin) {
                        caches.open(CACHE_NAME).then((cache) => {
                            cache.put(event.request, cloned);
                        });
                    }
                    return response;
                })
                .catch(() => {
                    if (event.request.mode === "navigate") {
                        return caches.match("/static/offline.html");
                    }
                    return new Response("", { status: 503, statusText: "Offline" });
                });
        })
    );
});
