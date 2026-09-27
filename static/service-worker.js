/*
    Service worker strategy:
    - Precache shell assets.
    - Stale-while-revalidate for runtime/static/API reads.
    - Network-first for HTML navigation, scripts and time-sensitive API reads,
      with the cached copy as the offline fallback.
    - Daily-study prewarm channel for Daf Yomi / Rambam / Parasha refs.
    - The siddur, whole, for offline use once a reader opens it.
*/

const CACHE_VERSION = "v23-20260925";
const SHELL_CACHE = `shelah-shell-${CACHE_VERSION}`;
const RUNTIME_CACHE = `shelah-runtime-${CACHE_VERSION}`;
const API_CACHE = `shelah-api-${CACHE_VERSION}`;
const PREWARM_CACHE = `shelah-prewarm-${CACHE_VERSION}`;

// The siddur's table of contents and every service (~600 KB gzipped), kept
// once a reader opens it (a PRECACHE_SIDDUR message from static/js/siddur.js)
// so it opens with no signal. Named for the siddur's own data version, not
// the deploy's CACHE_VERSION: a deploy that didn't rebuild the siddur keeps
// the copy, and a rebuild's version replaces it once whole. "@" can't occur
// in a slug, so one rite's name is never a prefix of another's.
const SIDDUR_CACHE_PREFIX = "shelah-siddur-";
const SIDDUR_SLUG_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const SIDDUR_VERSION_RE = /^[0-9a-f]{6,64}$/;
const SIDDUR_MAX_SERVICES = 100;

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
    // Per-user conversation threads: caching them by URL would show one
    // user's transcript to the next person on a shared device.
    "/api/conversations",
    // Shared answers: stale-while-revalidate would keep serving an answer
    // after its owner revoked the link.
    "/api/public/answer",
];

// Answers that depend on the current time (today's zmanim, the Hebrew date,
// the day's learning, this week's parasha, the coming holidays):
// stale-while-revalidate would first hand back yesterday's copy after
// midnight or sunset, or last week's parasha after Shabbat. These go to the
// network first and use the cached copy only when offline.
const TIME_SENSITIVE_API_PREFIXES = [
    "/api/zmanim",
    "/api/daily-study",
    "/api/parasha",
    "/api/holidays",
];

function isTimeSensitiveApi(pathname) {
    return TIME_SENSITIVE_API_PREFIXES.some((prefix) => pathname.startsWith(prefix));
}

function shouldBypassApiCache(pathname) {
    return PRIVATE_API_PREFIXES.some((prefix) => pathname.startsWith(prefix));
}

function isCacheableResponse(response) {
    return Boolean(response) && response.ok && response.type !== "opaque";
}

// Sh'elah's JSON API endpoints (e.g. /api/text/<ref>) report "not found"
// as HTTP 200 with an `error` field in the body, not a 4xx/5xx status --
// isCacheableResponse() alone can't see that and would cache the failure
// as if it were a real hit, making a since-fixed lookup stay broken until
// the cache version bumps. Only used for the API cache; static/runtime
// assets are never JSON-with-error shaped.
async function isCacheableApiResponse(response) {
    if (!isCacheableResponse(response)) {
        return false;
    }
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.includes("application/json")) {
        return true;
    }
    try {
        const payload = await response.clone().json();
        if (payload && typeof payload === "object" && payload.error) {
            return false;
        }
    } catch (_err) {
        // Unparsable body -- fall back to the ok/type check already passed above.
    }
    return true;
}

async function cachePut(cacheName, request, response) {
    if (!isCacheableResponse(response)) {
        return;
    }
    const cache = await caches.open(cacheName);
    await cache.put(request, response.clone());
}

async function staleWhileRevalidate(request, cacheName, event, fallbackFactory, isCacheable = isCacheableResponse) {
    const cache = await caches.open(cacheName);
    const cached = await cache.match(request);

    const fetchAndRefresh = fetch(request)
        .then(async (response) => {
            // isCacheable may be a plain predicate or an async one
            // (isCacheableApiResponse); Promise.resolve() covers both.
            if (await Promise.resolve(isCacheable(response))) {
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

async function networkFirstApi(request, offlineResponse) {
    try {
        const fresh = await fetch(request);
        if (await isCacheableApiResponse(fresh)) {
            const cache = await caches.open(API_CACHE);
            await cache.put(request, fresh.clone());
        }
        return fresh;
    } catch (_err) {
        const cached = await (await caches.open(API_CACHE)).match(request);
        return cached || offlineResponse();
    }
}

// Code is network-first too (audit L-10): stale-while-revalidate would run
// the last deploy's module under this deploy's page for one load. The HTTP
// cache still answers a fresh copy without a round trip. Offline, this exact
// version comes from the cache, else the last version seen of that file.
function isScript(pathname) {
    return pathname.startsWith("/static/js/") && pathname.endsWith(".js");
}

async function networkFirstScript(request) {
    const cache = await caches.open(SHELL_CACHE);
    try {
        const fresh = await fetch(request);
        await cachePut(SHELL_CACHE, request, fresh);
        return fresh;
    } catch (_err) {
        return (await cache.match(request))
            || (await cache.match(request, { ignoreSearch: true }))
            || new Response("", { status: 503, statusText: "Offline" });
    }
}

// Paths static/js/router.js writes (backend/routes_spa_paths.py serves the
// same shell as "/" on each), tails included (`/chat/new/all/balanced`,
// `/history/chat/<id>`, `/signin`). Offline, a never-visited one falls back
// to the precached shell, whose router then reads the path.
const SPA_PATH_PATTERNS = [
    /^\/(?:text|prayer|community|answer|a|calendar|chat|siddur)\/[^/]/,
    /^\/history(?:\/?$|\/chat\/[^/])/,
    /^\/(?:signin|profile|settings|siddur)\/?$/,
];

function isSpaPath(pathname) {
    return SPA_PATH_PATTERNS.some((pattern) => pattern.test(pathname));
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
        if (isSpaPath(new URL(request.url).pathname)) {
            const shell = await caches.match("/");
            if (shell) {
                return shell;
            }
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
        urls.push(`/api/text/${encoded}?autotranslate=0`, `/api/text/${encoded}?autotranslate=1`);
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

function siddurCacheName(rite, version) {
    return `${SIDDUR_CACHE_PREFIX}${rite}@${version}`;
}

function isSiddurData(pathname) {
    return pathname.startsWith("/api/siddur/v2/toc/") || pathname.startsWith("/api/siddur/v2/service/");
}

async function matchSiddurCache(request) {
    const names = (await caches.keys()).filter((name) => name.startsWith(SIDDUR_CACHE_PREFIX));
    for (const name of names) {
        const hit = await (await caches.open(name)).match(request);
        if (hit) {
            return hit;
        }
    }
    return undefined;
}

// A service at ?v=<version> never changes, so the kept copy answers it
// outright; anything else (the table of contents) is read as usual, with the
// kept copy as the offline fallback.
async function siddurData(request, event, offlineResponse) {
    const url = new URL(request.url);
    if (url.pathname.startsWith("/api/siddur/v2/service/") && url.searchParams.has("v")) {
        const kept = await matchSiddurCache(request);
        if (kept) {
            return kept;
        }
    }
    return staleWhileRevalidate(request, API_CACHE, event,
        async () => (await matchSiddurCache(request)) || offlineResponse(), isCacheableApiResponse);
}

function isSiddurSlug(value) {
    return typeof value === "string" && SIDDUR_SLUG_RE.test(value);
}

async function precacheSiddur({ rite, version, services }) {
    if (!isSiddurSlug(rite) || typeof version !== "string" || !SIDDUR_VERSION_RE.test(version)
        || !Array.isArray(services)) {
        return;
    }
    const slugs = [...new Set(services.filter(isSiddurSlug))].slice(0, SIDDUR_MAX_SERVICES);
    const name = siddurCacheName(rite, version);
    const cache = await caches.open(name);
    // Validated above, and encoded anyway: nothing a message carries can
    // reach past its own path segment.
    const riteSegment = encodeURIComponent(rite);
    const versionParam = encodeURIComponent(version);
    const paths = [
        `/api/siddur/v2/toc/${riteSegment}`,
        ...slugs.map((slug) => `/api/siddur/v2/service/${riteSegment}/${encodeURIComponent(slug)}?v=${versionParam}`),
    ];
    let whole = true;
    // One at a time: the reader's own requests go first.
    for (const path of paths) {
        const url = new URL(path, self.location.origin).href;
        if (await cache.match(url)) {
            continue;
        }
        try {
            const response = await fetch(url, { credentials: "same-origin" });
            if (await isCacheableApiResponse(response)) {
                await cache.put(url, response.clone());
            } else {
                whole = false;
            }
        } catch {
            // Offline or refused: this version isn't whole yet; a later open retries.
            whole = false;
        }
    }
    if (!whole) {
        // Keep the previous version too until this one is complete.
        return;
    }
    const older = (await caches.keys())
        .filter((key) => key.startsWith(`${SIDDUR_CACHE_PREFIX}${rite}@`) && key !== name);
    await Promise.all(older.map((key) => caches.delete(key)));
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
                    .filter((key) => !expected.has(key) && !key.startsWith(SIDDUR_CACHE_PREFIX))
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
    } else if (data.type === "PRECACHE_SIDDUR") {
        event.waitUntil(precacheSiddur(data));
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

        const offline = () => new Response(JSON.stringify({ error: "Offline" }), {
            status: 503,
            statusText: "Offline",
            headers: { "Content-Type": "application/json" },
        });
        if (isTimeSensitiveApi(url.pathname)) {
            event.respondWith(networkFirstApi(request, offline));
            return;
        }
        if (isSiddurData(url.pathname)) {
            event.respondWith(siddurData(request, event, offline));
            return;
        }
        event.respondWith(staleWhileRevalidate(request, API_CACHE, event, offline, isCacheableApiResponse));
        return;
    }

    if (isScript(url.pathname)) {
        event.respondWith(networkFirstScript(request));
        return;
    }

    const cacheName = url.pathname.startsWith("/static/") ? SHELL_CACHE : RUNTIME_CACHE;
    event.respondWith(staleWhileRevalidate(request, cacheName, event));
});
