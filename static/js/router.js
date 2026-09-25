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
//
// Display keys set how the page looks rather than what it shows, so a shared
// link can carry the sender's reading setup: `lang` (site language),
// `layout` (reader layout), `vowels`/`taamim`/`translit` (0|1 toggles) and
// `size` (reader font px). They are modifiers, not views: they survive
// exclusive view pushes AND clearRoute (going home keeps them), and a value
// outside the allowed set is dropped rather than guessed at. Example:
// `/?text=Genesis.1&layout=hebrew&vowels=1&taamim=0&lang=he`.
//
// `a` is a public share link to one AI answer (`/a/<share token>`,
// backend/routes_answer_share.py): the read-only counterpart of the owner's
// `chat`. Anything that isn't token-shaped is dropped here, before it can
// reach a fetch URL. `history` ("1") is the signed-in history page.
//
// Paths (deep-link Phase 5). The main view lives in the path, everything else
// stays in the query:
//
//   /text/<ref>  /prayer/<name>  /answer/<uuid> (chat)  /a/<token>
//   /history     /calendar/<YYYY-MM-DD> (date, only when no view is open)
//
// e.g. `/text/Genesis.1?date=2026-09-25&lang=he`. `community` and the
// conversation keys have no path of their own. readRoute() parses the path
// first, then the query (the path wins on the key it names), so a route
// object round-trips through either spelling. backend/routes_spa_paths.py
// serves the SPA shell on these paths, so a cold open or refresh works.
// Old query links (`/?text=...`, `/?chat=...`, `/?a=...`) still read the
// same; installRouter() rewrites them to their path once, with replaceState.
//
// The router only owns `/` and the paths above. On any other page that
// serves the shell (`/settings`, `/profile`) a push that opens no view keeps
// the current path, so e.g. changing the language there doesn't navigate
// away.
const VIEW_KEYS = ["text", "prayer", "community", "chat", "a", "history"];
const OVERLAY_KEYS = ["date", "conversation", "cv"];
const DISPLAY_KEYS = ["lang", "layout", "vowels", "taamim", "translit", "size"];
const ROUTE_KEYS = [...VIEW_KEYS, ...OVERLAY_KEYS, ...DISPLAY_KEYS];
const PERSISTENT_KEYS = [...OVERLAY_KEYS, ...DISPLAY_KEYS];
const CONVERSATION_SIZES = Object.freeze(["mini", "overlay", "full"]);
const DEFAULT_CONVERSATION_SIZE = "overlay";
const READER_LAYOUTS = Object.freeze(["bilingual", "bilingual-reverse", "interleaved", "english", "hebrew"]);
const FONT_SIZE_RANGE = Object.freeze({ min: 14, max: 30 });
const SHARE_TOKEN_RE = /^[A-Za-z0-9_-]{16,64}$/;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

// Route key -> first path segment. Order matters: when a non-exclusive push
// leaves two view keys set, the first one here takes the path and the other
// stays in the query.
const PATH_SEGMENTS = Object.freeze({ text: "text", prayer: "prayer", chat: "answer", a: "a", history: "history" });
const PATH_KEYS = Object.keys(PATH_SEGMENTS);
const SEGMENT_KEYS = Object.freeze({ ...Object.fromEntries(PATH_KEYS.map((key) => [PATH_SEGMENTS[key], key])), calendar: "date" });

const TOGGLE_VALUES = ["0", "1"];
const DISPLAY_VALIDATORS = {
    lang: (v) => ["en", "he"].includes(v),
    layout: (v) => READER_LAYOUTS.includes(v),
    vowels: (v) => TOGGLE_VALUES.includes(v),
    taamim: (v) => TOGGLE_VALUES.includes(v),
    translit: (v) => TOGGLE_VALUES.includes(v),
    size: (v) => /^\d{2}$/.test(v) && Number(v) >= FONT_SIZE_RANGE.min && Number(v) <= FONT_SIZE_RANGE.max,
};

function normalizeRoute(route) {
    if ("a" in route && !SHARE_TOKEN_RE.test(route.a)) delete route.a;
    if (!route.conversation) {
        delete route.cv;
    } else if (!CONVERSATION_SIZES.includes(route.cv)) {
        route.cv = DEFAULT_CONVERSATION_SIZE;
    }
    for (const key of DISPLAY_KEYS) {
        if (key in route && !DISPLAY_VALIDATORS[key](route[key])) delete route[key];
    }
    return route;
}

function decodeSegment(value) {
    try {
        return decodeURIComponent(value);
    } catch (_) {
        return "";
    }
}

// Readable but unambiguous: `:` and `,` are legal in a path segment, so refs
// like `Shulchan Arukh, Orach Chayim 345:1` keep them; everything else that
// needs escaping (spaces, `/`, `?`, `#`, `%`) is escaped.
function encodeSegment(value) {
    return encodeURIComponent(value).replace(/%3A/gi, ":").replace(/%2C/gi, ",");
}

// Sefaria-style slugs for the two name-like path values. A text ref's spaces
// become `_` and its section numbers are joined with `.` ("Genesis 2" ->
// "Genesis.2", "Shulchan Arukh, Orach Chayim 345:1" ->
// "Shulchan_Arukh,_Orach_Chayim.345.1", "Berakhot 2a:5" -> "Berakhot.2a.5");
// a prayer name only swaps spaces for `_`. parsePath turns both back into the
// plain ref / name the rest of the app uses, and the older `%20` / `:` forms
// still parse unchanged.
const SECTION = String.raw`\d+[ab]?`;
const SECTIONS = String.raw`${SECTION}(?:[:.]${SECTION})*(?:-${SECTION}(?:[:.]${SECTION})*)?`;
const REF_SECTIONS_RE = new RegExp(String.raw`^(.+?) (${SECTIONS})$`, "i");
const SLUG_SECTIONS_RE = new RegExp(String.raw`^(.+?)\.(${SECTIONS})$`, "i");

function refToSlug(ref) {
    const text = String(ref).trim();
    const match = REF_SECTIONS_RE.exec(text);
    if (!match) return text.replace(/ /g, "_");
    return `${match[1].replace(/ /g, "_")}.${match[2].replace(/:/g, ".")}`;
}

function slugToRef(slug) {
    const text = String(slug).replace(/_/g, " ");
    const match = SLUG_SECTIONS_RE.exec(text);
    return match ? `${match[1]} ${match[2].replace(/\./g, ":")}` : text;
}

const PATH_SLUGS = Object.freeze({
    text: { write: refToSlug, read: slugToRef },
    prayer: { write: (name) => String(name).replace(/ /g, "_"), read: (slug) => slug.replace(/_/g, " ") },
});

// `/text/Genesis.1` -> { text: "Genesis 1" }. Anything the router doesn't own
// (`/`, `/settings`, `/text` with nothing after it, a `/history/extra`)
// parses to {}.
function parsePath(pathname) {
    const match = /^\/([a-z]+)(?:\/(.*))?$/.exec(String(pathname || ""));
    const key = match && Object.hasOwn(SEGMENT_KEYS, match[1]) ? SEGMENT_KEYS[match[1]] : null;
    if (!key) return {};
    const rest = match[2] === undefined ? "" : decodeSegment(match[2].replace(/\/+$/, ""));
    if (key === "history") return rest ? {} : { history: "1" };
    if (!rest) return {};
    if (key === "date" && !DATE_RE.test(rest)) return {};
    return { [key]: PATH_SLUGS[key] ? PATH_SLUGS[key].read(rest) : rest };
}

function isRouterPath(pathname) {
    return pathname === "/" || Object.keys(parsePath(pathname)).length > 0;
}

function routeFrom(pathname, search) {
    const params = new URLSearchParams(search);
    const route = {};
    for (const key of ROUTE_KEYS) {
        const value = params.get(key);
        if (value) route[key] = value;
    }
    const fromPath = parsePath(pathname);
    // The path's view is the view: a stale view key left in the query can't
    // compete with it.
    if (PATH_KEYS.some((key) => key in fromPath)) {
        for (const key of VIEW_KEYS) delete route[key];
    }
    return normalizeRoute({ ...route, ...fromPath });
}

function readRoute() {
    return routeFrom(window.location.pathname, window.location.search);
}

// The path for a route: its first path-able view, else the calendar day,
// else `/` (or the current page, when that's one the router doesn't own).
function pathFor(route, currentPath = "/") {
    const key = PATH_KEYS.find((k) => route[k]);
    if (key === "history") return "/history";
    if (key) {
        const value = PATH_SLUGS[key] ? PATH_SLUGS[key].write(route[key]) : route[key];
        return `/${PATH_SEGMENTS[key]}/${encodeSegment(value)}`;
    }
    if (route.date && DATE_RE.test(route.date)) return `/calendar/${route.date}`;
    return isRouterPath(currentPath) ? "/" : currentPath;
}

function buildUrl(route, currentPath = "/") {
    const path = pathFor(route, currentPath);
    const inPath = parsePath(path);
    const params = new URLSearchParams();
    for (const key of ROUTE_KEYS) {
        if (key === "cv" && route.cv === DEFAULT_CONVERSATION_SIZE) continue;
        if (key in inPath) continue;
        if (route[key]) params.set(key, route[key]);
    }
    const qs = params.toString();
    return qs ? `${path}?${qs}` : path;
}

// A push to the URL already showing replaces it instead: re-opening the
// view you're on (a second click, a hydrate that re-renders the current
// route) must not stack a duplicate entry for Back to step through.
function writeHistory(route, replace) {
    try {
        const url = buildUrl(route, window.location.pathname);
        const same = url === `${window.location.pathname}${window.location.search}`;
        window.history[replace || same ? "replaceState" : "pushState"]({ shelahRoute: route }, "", url);
    } catch (_) {
        // Non-critical history update failure (e.g. sandboxed iframe).
    }
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
// `conversation`/`cv`) and display keys survive an exclusive push since they
// are independent of the view underneath them; pass them in `patch` to
// change them.
function pushRoute(patch, { replace = false, exclusive = false } = {}) {
    const current = readRoute();
    const next = exclusive ? pickKeys(current, PERSISTENT_KEYS) : { ...current };
    Object.entries(patch || {}).forEach(([key, value]) => {
        if (value === null || value === undefined || value === "") {
            delete next[key];
        } else {
            next[key] = String(value);
        }
    });
    normalizeRoute(next);
    writeHistory(next, replace);
    return next;
}

// Navigate to an in-app URL (`/answer/<id>`, `/text/Genesis.1?lang=he`, an
// old `/?chat=<id>`): parsed like a cold load, pushed in canonical form, and
// handed to the route handler. Returns the route. A URL the router doesn't
// own (another origin, `/about`) is left alone and returns null.
function pushPath(url, { replace = false } = {}) {
    let parsed;
    try {
        parsed = new URL(String(url), "http://router.invalid");
    } catch (_) {
        return null;
    }
    if (parsed.origin !== "http://router.invalid" && parsed.origin !== window.location.origin) return null;
    if (!isRouterPath(parsed.pathname)) return null;
    const next = routeFrom(parsed.pathname, parsed.search);
    writeHistory(next, replace);
    onRouteChange?.(next);
    return next;
}

// `keep` lists extra route keys to carry over (e.g. ["conversation", "cv"] to
// go home without closing an open conversation). Display keys are always
// kept -- going home doesn't undo the language/layout a link asked for.
function clearRoute({ replace = true, keep = [] } = {}) {
    const next = normalizeRoute(pickKeys(readRoute(), [...DISPLAY_KEYS, ...keep]));
    writeHistory(next, replace);
    return next;
}

// An old query link (`/?text=Genesis.1`) or a non-canonical path spelling
// becomes its canonical URL once, in place -- no new history entry, and the
// route (so the view) is unchanged. Pages the router doesn't own are left
// alone.
function canonicalizeUrl() {
    const { pathname, search } = window.location;
    if (!isRouterPath(pathname)) return;
    const route = readRoute();
    const url = buildUrl(route, pathname);
    if (url === pathname + search) return;
    try {
        window.history.replaceState({ ...(window.history.state || {}), shelahRoute: route }, "", url);
    } catch (_) {
        // Non-critical history update failure.
    }
}

let onRouteChange = null;

// The `popstate` state object is trusted first (it's what we wrote), and
// re-parsing `location.search` is only a fallback for entries this router
// didn't create itself (e.g. a bookmarked/shared URL loaded directly).
function installRouter(handler) {
    onRouteChange = typeof handler === "function" ? handler : null;
    canonicalizeUrl();
    window.addEventListener("popstate", (event) => {
        const route = (event.state && event.state.shelahRoute) || readRoute();
        onRouteChange?.(route);
    });
}

window.ShelahRouter = { readRoute, pushRoute, pushPath, clearRoute, installRouter, routeUrl: buildUrl, parsePath, VIEW_KEYS, DISPLAY_KEYS, CONVERSATION_SIZES, READER_LAYOUTS };

export { readRoute, pushRoute, pushPath, clearRoute, installRouter, buildUrl as routeUrl, parsePath, VIEW_KEYS, DISPLAY_KEYS, CONVERSATION_SIZES, READER_LAYOUTS };
