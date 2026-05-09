/*
    Service worker strategy:
    - Precache shell assets.
    - Stale-while-revalidate for runtime/static/API reads.
    - Network-first for HTML navigation with offline fallback.
    - Daily-study prewarm channel for Daf Yomi / Rambam / Parasha refs.
*/

const CACHE_VERSION = "v7";
const SHELL_CACHE = `shelah-shell-${CACHE_VERSION}`;
const RUNTIME_CACHE = `shelah-runtime-${CACHE_VERSION}`;
const API_CACHE = `shelah-api-${CACHE_VERSION}`;
const PREWARM_CACHE = `shelah-prewarm-${CACHE_VERSION}`;

const CORE_ASSETS = [
    "/",
    "/static/apple-touch-icon.png",
    "/static/favicon-192.png",
    "/static/favicon-512.png",
    "/static/style.css",
    "/static/offline.html",
    "/manifest.webmanifest",
    "/service-worker.js",
    "/static/js/main.js",
    "/static/js/state.js",
    "/static/js/ai-service.js",
    "/static/js/reader-ui.js",
    "/static/js/zmanim.js",
];

const PRIVATE_API_PREFIXES = [
    "/api/user/",
    "/api/bookmarks/",
    "/api/auth/",
    "/api/client-errors",
];

function shouldBypassApiCache(pathname) {
    return PRIVATE_API_PREFIXES.some((prefix) => pathname.startsWith(prefix));
}

function isCacheableResponse(response) {
    return Boolean(response) && response.ok && response.type !== "opaque";
}

async function cachePut(cacheName, request, response) {
    if (!isCacheableResponse(response)) {
        return;
    }
    const cache = await caches.open(cacheName);
    await cache.put(request, response.clone());
}

async function staleWhileRevalidate(request, cacheName, event, fallbackFactory) {
    const cache = await caches.open(cacheName);
    const cached = await cache.match(request);

    const fetchAndRefresh = fetch(request)
        .then(async (response) => {
            if (isCacheableResponse(response)) {
                await cache.put(request, response.clone());
            }
            return response;
        })
        .catch(() => null);

    if (cached) {
        if (event) {
            event.waitUntil(fetchAndRefresh);
        }
        return cached;
    }

    const fresh = await fetchAndRefresh;
    if (fresh) {
        return fresh;
    }

    if (typeof fallbackFactory === "function") {
        return fallbackFactory();
    }

    return new Response("", { status: 503, statusText: "Offline" });
}

async function networkFirstNavigation(request) {
    try {
        const fresh = await fetch(request);
        if (isCacheableResponse(fresh)) {
            await cachePut(RUNTIME_CACHE, request, fresh);
        }
        return fresh;
    } catch (_err) {
        const cache = await caches.open(RUNTIME_CACHE);
        const cached = await cache.match(request);
        if (cached) {
            return cached;
        }
        return caches.match("/static/offline.html");
    }
}

async function prewarmDailyRefs(refs) {
    if (!Array.isArray(refs) || !refs.length) {
        return;
    }

    const uniqueRefs = [];
    const seen = new Set();
    for (const rawRef of refs) {
        const ref = String(rawRef || "").trim();
        if (!ref || seen.has(ref)) {
            continue;
        }
        seen.add(ref);
        uniqueRefs.push(ref);
    }

    const urls = [];
    for (const ref of uniqueRefs.slice(0, 12)) {
        const encoded = encodeURIComponent(ref);
        urls.push(`/api/text/${encoded}?autotranslate=0`);
        urls.push(`/api/text/${encoded}?autotranslate=1`);
    }

    const cache = await caches.open(PREWARM_CACHE);
    await Promise.allSettled(
        urls.map(async (url) => {
            try {
                const response = await fetch(url, { credentials: "same-origin" });
                if (isCacheableResponse(response)) {
                    await cache.put(url, response.clone());
                }
            } catch (_err) {
                // Ignore individual prewarm failures.
            }
        })
    );
}

self.addEventListener("install", (event) => {
    event.waitUntil(
        caches.open(SHELL_CACHE).then((cache) => cache.addAll(CORE_ASSETS))
    );
    self.skipWaiting();
});

self.addEventListener("activate", (event) => {
    const expected = new Set([SHELL_CACHE, RUNTIME_CACHE, API_CACHE, PREWARM_CACHE]);
    event.waitUntil(
        caches.keys().then((keys) => {
            return Promise.all(
                keys
                    .filter((key) => !expected.has(key))
                    .map((key) => caches.delete(key))
            );
        })
    );
    self.clients.claim();
});

self.addEventListener("message", (event) => {
    const data = event.data || {};
    if (data.type === "PREWARM_DAILY") {
        const refs = Array.isArray(data.refs) ? data.refs : [];
        event.waitUntil(prewarmDailyRefs(refs));
    }
});

self.addEventListener("fetch", (event) => {
    const request = event.request;
    if (request.method !== "GET") {
        return;
    }

    const url = new URL(request.url);
    const isSameOrigin = url.origin === self.location.origin;
    if (!isSameOrigin) {
        return;
    }

    if (request.mode === "navigate") {
        event.respondWith(networkFirstNavigation(request));
        return;
    }

    if (url.pathname.startsWith("/api/")) {
        if (shouldBypassApiCache(url.pathname)) {
            event.respondWith(
                fetch(request).catch(() => {
                    return new Response(JSON.stringify({ error: "Offline" }), {
                        status: 503,
                        statusText: "Offline",
                        headers: { "Content-Type": "application/json" },
                    });
                })
            );
            return;
        }

        event.respondWith(
            staleWhileRevalidate(request, API_CACHE, event, () => {
                return new Response(JSON.stringify({ error: "Offline" }), {
                    status: 503,
                    statusText: "Offline",
                    headers: { "Content-Type": "application/json" },
                });
            })
        );
        return;
    }

    const cacheName = url.pathname.startsWith("/static/") ? SHELL_CACHE : RUNTIME_CACHE;
    event.respondWith(staleWhileRevalidate(request, cacheName, event));
});
