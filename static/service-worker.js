/*
    Service worker for offline-first behavior.

    Responsibilities:
    - Precache core shell assets (home, CSS, offline page, manifest).
    - Serve cached responses first, then network.
    - Cache successful same-origin GET responses on demand.
    - Return /static/offline.html when navigation requests fail offline.
*/

const CACHE_NAME = "shelah-cache-v1";
const CORE_ASSETS = [
    "/",
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

    event.respondWith(
        caches.match(event.request).then((cached) => {
            if (cached) {
                return cached;
            }

            return fetch(event.request)
                .then((response) => {
                    const cloned = response.clone();
                    if (response.ok && event.request.url.startsWith(self.location.origin)) {
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
