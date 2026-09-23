/**
 * sentry-init.js — Sh'elah Browser Error Monitoring
 *
 * Wires up the Sentry Browser SDK (loaded separately from Sentry's own CDN
 * with an SRI hash, see templates/index.html) with the privacy and quota
 * discipline plan.md §17 requires:
 *
 * - True no-op when SENTRY_DSN_BROWSER is unset or the CDN script failed to
 *   load: no throw, no console warning, mirrors static/js/motion.js.
 * - sendDefaultPii: false + dataCollection{userInfo:false, httpBodies:[]} —
 *   the vendor-supported mechanism for keeping question/answer text out of
 *   Sentry. beforeSend/beforeBreadcrumb below are defence-in-depth on top of
 *   that, not a substitute for it (plan.md §17.3 deviation 5).
 * - No Replay integration (this file only ever calls Sentry.init on the
 *   plain errors-only CDN bundle — see index.html's <script> comment).
 * - tracesSampleRate: 0 — errors only; tracing spend is a §14 cost decision.
 * - Noise filtering + a per-session cap, protecting the 5,000 events/month
 *   free-tier quota (plan.md §17.1).
 *
 * Loaded as a plain (non-module) script, synchronously, after the Sentry CDN
 * bundle and before any other app script — so it can capture errors from
 * everything that loads after it. Written as a small UMD-lite module (no
 * import/export syntax) so the exact same file can be `require()`d from a
 * Node test (tests_js/sentry_init.test.js) without a build step.
 */
(function (root) {
    'use strict';

    var ASK_PATH_RE = /\/ask(?:$|[/?#])/;
    var RESIZE_OBSERVER_RE = /ResizeObserver loop/i;
    var NAV_ABORT_RE = /(the user aborted a request|load failed|network request failed|failed to fetch)/i;
    var EXTENSION_ORIGIN_RE = /^(chrome|moz|safari|ms-browser)-extension:\/\//i;

    // Any of these keys, wherever they show up in a breadcrumb/event payload,
    // could carry question text, an AI answer, or a raw request/response
    // body. Halachic questions are frequently medical, marital, mental-
    // health, or abuse-adjacent (plan.md §8.B/§8.D) — that text must never
    // reach a third-party error tracker.
    var SENSITIVE_HEADER_KEYS = ['authorization', 'cookie', 'set-cookie', 'x-clerk-auth-token', '__session'];
    // Matches app-chosen key names loosely (event.extra / event.contexts are
    // free-form dicts a developer might populate as "last_answer",
    // "ai_ruling", "userQuestion", etc.) — substring match, not exact,
    // because we can't predict every naming variant in advance.
    var SENSITIVE_KEY_SUBSTRINGS = ['question', 'answer', 'ruling', 'summary', 'practical_step', 'body', 'text'];

    function isSensitiveKey(key) {
        var lower = String(key).toLowerCase();
        return SENSITIVE_KEY_SUBSTRINGS.some(function (s) { return lower.indexOf(s) !== -1; });
    }

    var MAX_EVENTS_PER_SESSION = 40;
    var DEDUPE_WINDOW_MS = 30000;

    // ── Pure helpers ─────────────────────────────────────────────────────────

    function stripQuery(url) {
        if (typeof url !== 'string' || !url) return url;
        var idx = url.search(/[?#]/);
        return idx === -1 ? url : url.slice(0, idx);
    }

    function isAskUrl(url) {
        return typeof url === 'string' && ASK_PATH_RE.test(url);
    }

    function scrubHeaders(headers) {
        if (!headers || typeof headers !== 'object') return headers;
        var out = {};
        Object.keys(headers).forEach(function (key) {
            out[key] = SENSITIVE_HEADER_KEYS.indexOf(key.toLowerCase()) !== -1
                ? '[Filtered]'
                : headers[key];
        });
        return out;
    }

    function scrubBreadcrumb(breadcrumb) {
        if (!breadcrumb || typeof breadcrumb !== 'object') return breadcrumb;
        var category = breadcrumb.category || '';
        var data = breadcrumb.data;

        // Console breadcrumbs carry arbitrary free text (console.log/.error
        // arguments) — there's no reliable structural way to tell "just a
        // debug string" from "a halachic question a developer logged for
        // debugging," so the whole message/data is redacted rather than
        // pattern-matched. Category/level/timestamp metadata survives.
        if (category === 'console') {
            if ('message' in breadcrumb) breadcrumb.message = '[Filtered]';
            if (data) breadcrumb.data = '[Filtered]';
            return breadcrumb;
        }

        if (data && typeof data === 'object') {
            var url = typeof data.url === 'string' ? data.url : '';
            var isHttpBreadcrumb = category === 'xhr' || category === 'fetch';
            if (isHttpBreadcrumb || isAskUrl(url)) {
                // Sentry's browser SDK doesn't attach request/response bodies
                // to xhr/fetch breadcrumbs by default, and dataCollection.
                // httpBodies=[] blocks it at the source — this survives a
                // future SDK/config change that starts doing so anyway.
                Object.keys(data).forEach(function (key) {
                    if (isSensitiveKey(key)) data[key] = '[Filtered]';
                });
            }
            if (typeof data.url === 'string') data.url = stripQuery(data.url);
            if (typeof data.to === 'string') data.to = stripQuery(data.to);
            if (data.headers) data.headers = scrubHeaders(data.headers);
        }

        if (typeof breadcrumb.message === 'string') {
            breadcrumb.message = isAskUrl(breadcrumb.message) ? '[Filtered]' : stripQuery(breadcrumb.message);
        }

        return breadcrumb;
    }

    function exceptionValues(event) {
        return (event && event.exception && event.exception.values) || [];
    }

    function isResizeObserverNoise(event) {
        if (RESIZE_OBSERVER_RE.test(event.message || '')) return true;
        return exceptionValues(event).some(function (v) {
            return RESIZE_OBSERVER_RE.test(v.value || '');
        });
    }

    function isExtensionOriginNoise(event) {
        return exceptionValues(event).some(function (v) {
            var frames = (v.stacktrace && v.stacktrace.frames) || [];
            return frames.some(function (f) { return EXTENSION_ORIGIN_RE.test(f.filename || ''); });
        });
    }

    function isNavigationAbortNoise(event) {
        if (NAV_ABORT_RE.test(event.message || '')) return true;
        return exceptionValues(event).some(function (v) {
            return NAV_ABORT_RE.test(v.value || '');
        });
    }

    // Drops an event only when EVERY frame of EVERY exception value is
    // definitively off-origin — conservative, so a same-origin error that
    // happens to pass through one third-party callback frame still reports.
    function isOffOriginNoise(event, allowedOrigin) {
        if (!allowedOrigin) return false;
        var values = exceptionValues(event);
        if (!values.length) return false;
        return values.every(function (v) {
            var frames = (v.stacktrace && v.stacktrace.frames) || [];
            if (!frames.length) return false;
            return frames.every(function (f) {
                var fn = f.filename || '';
                if (!fn || fn.indexOf('://') === -1) return false; // relative/inline — same-origin
                try {
                    return new URL(fn).origin !== allowedOrigin;
                } catch (e) {
                    return false;
                }
            });
        });
    }

    // Sentry's own default is 250 — kept here explicitly (not relied on
    // implicitly) as a backstop against free-text error messages that echo
    // application content. This is a partial mitigation, not a guarantee:
    // application code must never interpolate question/answer text into a
    // thrown Error's message in the first place (that's a main.js-level
    // discipline issue, not something a generic scrubber can fully reverse).
    var MAX_FREE_TEXT_LENGTH = 250;

    function truncate(value) {
        if (typeof value !== 'string' || value.length <= MAX_FREE_TEXT_LENGTH) return value;
        return value.slice(0, MAX_FREE_TEXT_LENGTH) + '… [truncated]';
    }

    function scrubEventInPlace(event) {
        if (typeof event.message === 'string') event.message = truncate(event.message);
        exceptionValues(event).forEach(function (v) {
            if (typeof v.value === 'string') v.value = truncate(v.value);
        });
        if (event.request) {
            delete event.request.data;
            delete event.request.cookies;
            if (event.request.headers) event.request.headers = scrubHeaders(event.request.headers);
            if (typeof event.request.url === 'string') event.request.url = stripQuery(event.request.url);
        }
        if (Array.isArray(event.breadcrumbs)) {
            event.breadcrumbs = event.breadcrumbs.map(scrubBreadcrumb);
        }
        if (event.extra && typeof event.extra === 'object') {
            Object.keys(event.extra).forEach(function (key) {
                if (isSensitiveKey(key)) {
                    event.extra[key] = '[Filtered]';
                }
            });
        }
        if (event.contexts && typeof event.contexts === 'object') {
            Object.keys(event.contexts).forEach(function (ctxKey) {
                var ctx = event.contexts[ctxKey];
                if (ctx && typeof ctx === 'object') {
                    Object.keys(ctx).forEach(function (key) {
                        if (isSensitiveKey(key)) ctx[key] = '[Filtered]';
                    });
                }
            });
        }
        return event;
    }

    // ── Factories (closures hold per-session throttle/dedupe state) ─────────

    function makeBeforeSend(allowedOrigin) {
        var seenAt = Object.create(null);
        var sentCount = 0;

        return function beforeSend(event) {
            if (!event || typeof event !== 'object') return event;
            if (isResizeObserverNoise(event)) return null;
            if (isExtensionOriginNoise(event)) return null;
            if (isNavigationAbortNoise(event)) return null;
            if (isOffOriginNoise(event, allowedOrigin)) return null;

            var firstValue = exceptionValues(event)[0];
            var fingerprint = event.message || (firstValue && firstValue.value) || 'unknown';
            var now = Date.now();
            if (seenAt[fingerprint] && (now - seenAt[fingerprint]) < DEDUPE_WINDOW_MS) {
                return null; // same error looping — drop repeats within the window
            }
            seenAt[fingerprint] = now;

            sentCount += 1;
            if (sentCount > MAX_EVENTS_PER_SESSION) {
                return null; // session-level throttle — protects the 5K/month quota
            }

            return scrubEventInPlace(event);
        };
    }

    function makeBeforeBreadcrumb() {
        return function beforeBreadcrumb(breadcrumb) {
            return scrubBreadcrumb(breadcrumb);
        };
    }

    // ── Browser bootstrap (never runs under Node/`require`) ─────────────────

    function initSentry() {
        var Sentry = root.Sentry;
        var dsn = root.__SHELAH_SENTRY_DSN__;
        if (!Sentry || typeof Sentry.init !== 'function' || !dsn) {
            return; // CDN failed, or DSN unset — silent no-op, site works normally
        }

        var allowedOrigin = (root.location && root.location.origin) || '';
        var environment = root.__SHELAH_SENTRY_ENV__ || 'development';
        var release = root.__SHELAH_SENTRY_RELEASE__ || undefined;

        try {
            Sentry.init({
                dsn: dsn,
                environment: environment,
                release: release,
                // Overrides the vendor-suggested default (plan.md §17.3
                // deviation 1) — request headers/cookies/IP must never be
                // attached automatically.
                sendDefaultPii: false,
                tracesSampleRate: 0,
                dataCollection: {
                    userInfo: false,
                    httpBodies: [],
                },
                beforeSend: makeBeforeSend(allowedOrigin),
                beforeBreadcrumb: makeBeforeBreadcrumb(),
            });
        } catch (e) {
            // Error-monitoring setup must never break the app itself.
        }
    }

    var api = {
        stripQuery: stripQuery,
        isAskUrl: isAskUrl,
        scrubHeaders: scrubHeaders,
        scrubBreadcrumb: scrubBreadcrumb,
        scrubEventInPlace: scrubEventInPlace,
        isResizeObserverNoise: isResizeObserverNoise,
        isExtensionOriginNoise: isExtensionOriginNoise,
        isNavigationAbortNoise: isNavigationAbortNoise,
        isOffOriginNoise: isOffOriginNoise,
        makeBeforeSend: makeBeforeSend,
        makeBeforeBreadcrumb: makeBeforeBreadcrumb,
        initSentry: initSentry,
    };

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;
    } else {
        root.ShelahSentryInit = api;
        initSentry();
    }
})(typeof window !== 'undefined' ? window : globalThis);
