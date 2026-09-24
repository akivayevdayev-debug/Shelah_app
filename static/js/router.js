// Client-side deep-link router for the SPA's classic inline script.
//
// The app is a single page (`templates/index.html`) with one huge classic
// `<script>` block that owns all view state (text/prayer/community/ai
// answers, the calendar overlay). This module only tracks `location.search`
// and history entries -- it has no knowledge of the app's own render
// functions. The classic script wires itself in via `window.hydrateRoute`
// (installed by main.js's `installRouter` call) and calls `pushRoute`/
// `clearRoute` from its own view-switching functions.
//
// `date` is a separate key from the four mutually-exclusive view keys
// because the calendar renders as an overlay on top of whatever view is
// active underneath it (plan.md "implementation brief" §2).
//
// `conversation` (a multi-turn conversation id) + `cv` (its display size) are
// overlay keys for the same reason: the conversation UI sits on top of the
// view underneath it -- the mini widget must stay open while a cited text
// opens under it, and closing the full page returns to that view -- so they
// survive exclusive view pushes just like `date`. (`chat` is unrelated: the
// legacy single-answer history view.) Deep link: `/?conversation=<id>&cv=full`.
// `cv` means nothing without `conversation` and is dropped; a missing or
// unknown `cv` reads as "overlay", the default, which the URL leaves implicit.
const VIEW_KEYS = ["text", "prayer", "community", "chat"];
const OVERLAY_KEYS = ["date", "conversation", "cv"];
const ROUTE_KEYS = [...VIEW_KEYS, ...OVERLAY_KEYS];
const CONVERSATION_SIZES = Object.freeze(["mini", "overlay", "full"]);
const DEFAULT_CONVERSATION_SIZE = "overlay";

function normalizeRoute(route) {
    if (!route.conversation) {
        delete route.cv;
    } else if (!CONVERSATION_SIZES.includes(route.cv)) {
        route.cv = DEFAULT_CONVERSATION_SIZE;
    }
    return route;
}

function readRoute() {
    const params = new URLSearchParams(window.location.search);
    const route = {};
    for (const key of ROUTE_KEYS) {
        const value = params.get(key);
        if (value) route[key] = value;
    }
    return normalizeRoute(route);
}

function buildSearch(route) {
    const params = new URLSearchParams();
    for (const key of ROUTE_KEYS) {
        if (key === "cv" && route.cv === DEFAULT_CONVERSATION_SIZE) continue;
        if (route[key]) params.set(key, route[key]);
    }
    const qs = params.toString();
    return qs ? `?${qs}` : "";
}

function pickKeys(route, keys) {
    const picked = {};
    for (const key of keys) {
        if (route[key]) picked[key] = route[key];
    }
    return picked;
}

// `exclusive` drops every other view key before applying `patch` -- used
// when switching views (text -> prayer, etc.) so the URL never accumulates
// stale keys from a previously-visited view. The overlay keys (`date`,
// `conversation`/`cv`) survive an exclusive push since those overlays are
// independent of the view underneath them; pass them in `patch` to change them.
function pushRoute(patch, { replace = false, exclusive = false } = {}) {
    const current = readRoute();
    const next = exclusive ? pickKeys(current, OVERLAY_KEYS) : { ...current };
    Object.entries(patch || {}).forEach(([key, value]) => {
        if (value === null || value === undefined || value === "") {
            delete next[key];
        } else {
            next[key] = String(value);
        }
    });
    normalizeRoute(next);
    const url = window.location.pathname + buildSearch(next);
    try {
        window.history[replace ? "replaceState" : "pushState"]({ shelahRoute: next }, "", url);
    } catch (_) {
        // Non-critical history update failure (e.g. sandboxed iframe).
    }
    return next;
}

// `keep` lists route keys to carry over (e.g. ["conversation", "cv"] to go
// home without closing an open conversation); the default clears everything.
function clearRoute({ replace = true, keep = [] } = {}) {
    const next = keep.length ? normalizeRoute(pickKeys(readRoute(), keep)) : {};
    try {
        window.history[replace ? "replaceState" : "pushState"]({ shelahRoute: next }, "", window.location.pathname + buildSearch(next));
    } catch (_) {
        // Non-critical history update failure.
    }
    return next;
}

let onRouteChange = null;

// The `popstate` state object is trusted first (it's what we wrote), and
// re-parsing `location.search` is only a fallback for entries this router
// didn't create itself (e.g. a bookmarked/shared URL loaded directly).
function installRouter(handler) {
    onRouteChange = typeof handler === "function" ? handler : null;
    window.addEventListener("popstate", (event) => {
        const route = (event.state && event.state.shelahRoute) || readRoute();
        onRouteChange?.(route);
    });
}

window.ShelahRouter = { readRoute, pushRoute, clearRoute, installRouter, VIEW_KEYS, CONVERSATION_SIZES };

export { readRoute, pushRoute, clearRoute, installRouter, VIEW_KEYS, CONVERSATION_SIZES };
