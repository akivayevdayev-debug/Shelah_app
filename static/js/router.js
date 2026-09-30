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
// legacy single-answer history view.) Deep link: `/chat/<id>/full`.
// `cv` means nothing without `conversation` and is dropped; a missing or
// unknown `cv` reads as "overlay", the default, which the URL leaves implicit.
//
// Display keys set how the page looks rather than what it shows, so a shared
// link can carry the sender's reading setup: `lang` (site language),
// `layout` (reader layout), `vowels`/`taamim`/`translit` (0|1 toggles) and
// `size` (reader font px). They are modifiers, not views: they survive
// exclusive view pushes AND clearRoute (going home keeps them), and a value
// outside the allowed set is dropped rather than guessed at. Example:
// `/text/Genesis.1?layout=hebrew&vowels=1&taamim=0&lang=he`.
//
// AI keys: `minhag` (the community the answer is shaped for) and `mode` (the
// AI mode) say how the open AI surface -- a conversation (`conversation`) or
// an answer (`chat` / `a`) -- is answering, e.g.
// `/answer/<id>/sefardic/strict`. They ride along with that
// surface: kept through exclusive view pushes like the overlay keys, and
// dropped whenever no conversation or answer is in the route. A mode outside
// AI_MODES or a minhag that isn't a plain name is dropped, not guessed at.
//
// `a` is a public share link to one AI answer (`/a/<share token>`,
// backend/routes_answer_share.py): the read-only counterpart of the owner's
// `chat`. Anything that isn't token-shaped is dropped here, before it can
// reach a fetch URL. `history` ("1") is the signed-in history page.
//
// Paths (deep-link Phase 5, formal URLs). Everything the app itself writes
// lives in the path; only a shared link's display keys and the history
// search stay in the query. A path is an optional view, then tail segments:
//
//   view:  /text/<ref>  /prayer/<name>  /community/<name>  /answer/<uuid> (chat)
//          /a/<token>   /history
//          /siddur/<rite>[/<service>[/<section>]]  (up to three slugs; the
//          route value joins them with `/`, e.g. "edot-hamizrach/shacharit/amida")
//   tail:  chat/<id>              the conversation (`new` for a fresh one)
//          <community> <mode>     the AI keys, e.g. `sefardic/strict`
//          full | mini            the conversation's size (overlay is implicit)
//          calendar/<YYYY-MM-DD>  the calendar day
//          signin | profile | settings   the account modal (`auth`)
//
// e.g. `/chat/new/all/balanced`, `/answer/<uuid>/sefardic/strict`,
// `/text/Genesis.1/chat/<uuid>/ashkenaz/sources/mini`,
// `/text/Genesis.1/calendar/2026-09-25?lang=he`, `/signin`. The tail's words
// only mean something in these places: a community or mode needs a
// conversation or an answer beside it, a size needs a conversation, and any
// segment that fits nowhere makes the path one the router doesn't own.
// readRoute() parses the path first, then the query (the path wins on the
// key it names), so a route object round-trips through either spelling.
// backend/routes_spa_paths.py serves the SPA shell on these paths, so a cold
// open or refresh works. Old query links (`/?text=...`, `/?conversation=...`,
// `/answer/<id>?minhag=...`) still read the same; installRouter() rewrites
// them to their path once, with replaceState.
//
// `auth` (signin | profile | settings) is an overlay like the calendar: the
// Clerk modal it names opens over whatever view is under it
// (`/text/Genesis.1/signin`), and closing the modal steps back off it
// (closeOverlay). A push that opens no view on a page the router doesn't own
// keeps the current path.
//
// History entries. Every entry the router writes carries `seq` (its depth:
// a push is one more than the entry it was pushed from, a replace keeps it),
// so a popstate can tell Back from Forward. Overlays that sit over a view --
// an answer (`chat`/`a`), the conversation, the calendar -- follow the
// overlay contract: opening one pushes an entry marked
// `{ overlay: <kind>, pushedBy: "app" }`, and closing it with closeOverlay()
// steps Back off that entry when the app pushed it (so Back never re-opens
// what was just closed, and the entry underneath is the view as it was),
// or replaces the URL when it didn't (a cold-loaded or shared link has no
// entry of ours underneath to go back to). A replace keeps the marker while
// its overlay is still in the route; a new marker is only ever written by a
// real push.
const VIEW_KEYS = ["text", "prayer", "community", "siddur", "chat", "a", "history"];
// A view's own state, dropped with the view: `q` is the history page's
// search (`/history?q=`, audit U-15).
const VIEW_PARAM_KEYS = ["q"];
const MAX_HISTORY_QUERY_CHARS = 200;
const OVERLAY_KEYS = ["date", "conversation", "cv", "auth"];
const AI_KEYS = ["minhag", "mode"];
const DISPLAY_KEYS = ["lang", "layout", "vowels", "taamim", "translit", "size"];
const ROUTE_KEYS = [...VIEW_KEYS, ...VIEW_PARAM_KEYS, ...OVERLAY_KEYS, ...AI_KEYS, ...DISPLAY_KEYS];
const PERSISTENT_KEYS = [...OVERLAY_KEYS, ...AI_KEYS, ...DISPLAY_KEYS];
const CONVERSATION_SIZES = Object.freeze(["mini", "overlay", "full"]);
const DEFAULT_CONVERSATION_SIZE = "overlay";
const AI_MODES = Object.freeze(["balanced", "practical", "sources", "strict"]);
const MINHAG_RE = /^[A-Za-z][A-Za-z-]{0,39}$/;
const AUTH_PAGES = Object.freeze(["signin", "profile", "settings"]);
// Bare tail words -> the key they set. A community is any other plain word.
const TAIL_WORDS = Object.freeze({
    ...Object.fromEntries(CONVERSATION_SIZES.map((size) => [size, "cv"])),
    ...Object.fromEntries(AI_MODES.map((mode) => [mode, "mode"])),
    ...Object.fromEntries(AUTH_PAGES.map((page) => [page, "auth"])),
});
const TAIL_PAIRS = Object.freeze({ chat: "conversation", calendar: "date" });
const READER_LAYOUTS = Object.freeze(["bilingual", "bilingual-reverse", "interleaved", "english", "hebrew"]);
const FONT_SIZE_RANGE = Object.freeze({ min: 14, max: 30 });
const SHARE_TOKEN_RE = /^[A-Za-z0-9_-]{16,64}$/;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

// Route key -> first path segment. Order matters: when a non-exclusive push
// leaves two view keys set, the first one here takes the path and the other
// stays in the query.
const PATH_SEGMENTS = Object.freeze({ text: "text", prayer: "prayer", community: "community", siddur: "siddur", chat: "answer", a: "a", history: "history" });
const PATH_KEYS = Object.keys(PATH_SEGMENTS);
const SEGMENT_KEYS = Object.freeze(Object.fromEntries(PATH_KEYS.map((key) => [PATH_SEGMENTS[key], key])));
// Words a community's slug can't be: it would read back as something else.
const RESERVED_WORDS = new Set([...Object.keys(TAIL_WORDS), ...Object.keys(TAIL_PAIRS), ...Object.keys(SEGMENT_KEYS)]);
// The siddur (/siddur/<rite>[/<service>[/<section>]]): lowercase slugs that
// scripts/build_siddur.py keeps clear of RESERVED_WORDS, so the tail after
// them still reads. `/siddur` alone is the default rite's contents.
const SIDDUR_SLUG_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const SIDDUR_VALUE_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*(?:\/[a-z0-9]+(?:-[a-z0-9]+)*){0,2}$/;
const DEFAULT_SIDDUR_RITE = "edot-hamizrach";

// A community in a path is its lowercase slug (`greek-romaniote`); every
// community name is title case per hyphenated part, so the slug reads back.
function minhagName(value) {
    return String(value).toLowerCase().replace(/(^|-)([a-z])/g, (_, dash, ch) => dash + ch.toUpperCase());
}

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
    if ("siddur" in route && !SIDDUR_VALUE_RE.test(route.siddur)) delete route.siddur;
    if ("q" in route) {
        const q = route.history ? String(route.q).replace(/\s+/g, " ").trim().slice(0, MAX_HISTORY_QUERY_CHARS) : "";
        if (q) route.q = q;
        else delete route.q;
    }
    if (!route.conversation) {
        delete route.cv;
    } else if (!CONVERSATION_SIZES.includes(route.cv)) {
        route.cv = DEFAULT_CONVERSATION_SIZE;
    }
    if (!route.conversation && !route.chat && !route.a) {
        delete route.minhag;
        delete route.mode;
    }
    if ("minhag" in route) {
        if (MINHAG_RE.test(route.minhag) && !RESERVED_WORDS.has(route.minhag.toLowerCase())) route.minhag = minhagName(route.minhag);
        else delete route.minhag;
    }
    if ("mode" in route && !AI_MODES.includes(route.mode)) delete route.mode;
    if ("auth" in route && !AUTH_PAGES.includes(route.auth)) delete route.auth;
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

// Sefaria-style slugs for the name-like path values. A text ref's spaces
// become `_` and its section numbers are joined with `.` ("Genesis 2" ->
// "Genesis.2", "Shulchan Arukh, Orach Chayim 345:1" ->
// "Shulchan_Arukh,_Orach_Chayim.345.1", "Berakhot 2a:5" -> "Berakhot.2a.5");
// a prayer or community name only swaps spaces for `_`. parsePath turns both back into the
// plain ref / name the rest of the app uses, and the older `%20` / `:` forms
// still parse unchanged.
const SECTION = String.raw`\d+[ab]?`;
const SECTIONS = String.raw`${SECTION}(?:[:.]${SECTION})*(?:-${SECTION}(?:[:.]${SECTION})*)?`;
const REF_SECTIONS_RE = new RegExp(String.raw`^(.+?) (${SECTIONS})$`, "i");
const SLUG_SECTIONS_RE = new RegExp(String.raw`^(.+?)\.(${SECTIONS})$`, "i");

function refToSlug(ref) {
    const text = String(ref).trim();
    const match = REF_SECTIONS_RE.exec(text);
    if (!match) return text.replaceAll(" ", "_");
    return `${match[1].replaceAll(" ", "_")}.${match[2].replaceAll(":", ".")}`;
}

function slugToRef(slug) {
    const text = String(slug).replaceAll("_", " ");
    const match = SLUG_SECTIONS_RE.exec(text);
    return match ? `${match[1]} ${match[2].replaceAll(".", ":")}` : text;
}

const NAME_SLUG = Object.freeze({ write: (name) => String(name).replaceAll(" ", "_"), read: (slug) => slug.replaceAll("_", " ") });
const PATH_SLUGS = Object.freeze({
    text: { write: refToSlug, read: slugToRef },
    prayer: NAME_SLUG,
    community: NAME_SLUG,
});

// `/text/Genesis.1` -> { text: "Genesis 1" },
// `/chat/new/sefardic/strict` -> { conversation: "new", minhag: "Sefardic",
// mode: "strict" }. Anything the router doesn't own (`/`, `/about`, `/text`
// with nothing after it, a `/history/extra`) parses to {}.
function parsePath(pathname) {
    const path = String(pathname || "");
    if (!path.startsWith("/")) return {};
    let bare = path.slice(1);
    while (bare.endsWith("/")) bare = bare.slice(0, -1);
    const parts = bare.split("/");
    if (parts.length === 1 && !parts[0]) return {};
    const route = {};
    let i = 0;
    const view = Object.hasOwn(SEGMENT_KEYS, parts[0]) ? SEGMENT_KEYS[parts[0]] : null;
    if (view === "history") {
        route.history = "1";
        i = 1;
    } else if (view === "siddur") {
        const slugs = [];
        i = 1;
        while (i < parts.length && slugs.length < 3) {
            const word = decodeSegment(parts[i]);
            if (!SIDDUR_SLUG_RE.test(word) || RESERVED_WORDS.has(word)) break;
            slugs.push(word);
            i += 1;
        }
        route.siddur = slugs.length ? slugs.join("/") : DEFAULT_SIDDUR_RITE;
    } else if (view) {
        const raw = parts[1] === undefined ? "" : decodeSegment(parts[1]);
        if (!raw) return {};
        // A `+` in a path is a literal plus (we never write one), but people
        // and other sites type `/text/Genesis+1` meaning a space. No ref or
        // prayer name contains a plus, so it reads as one.
        route[view] = PATH_SLUGS[view] ? PATH_SLUGS[view].read(raw.replaceAll("+", " ")) : raw;
        i = 2;
    }
    while (i < parts.length) {
        const word = decodeSegment(parts[i]);
        let key;
        let value;
        if (Object.hasOwn(TAIL_PAIRS, word)) {
            key = TAIL_PAIRS[word];
            value = parts[i + 1] === undefined ? "" : decodeSegment(parts[i + 1]);
            if (!value || (key === "date" && !DATE_RE.test(value))) return {};
            i += 2;
        } else if (Object.hasOwn(TAIL_WORDS, word.toLowerCase())) {
            key = TAIL_WORDS[word.toLowerCase()];
            value = word.toLowerCase();
            i += 1;
        } else if (MINHAG_RE.test(word)) {
            key = "minhag";
            value = minhagName(word);
            i += 1;
        } else {
            return {};
        }
        if (key in route) return {};
        route[key] = value;
    }
    if ("cv" in route && !route.conversation) return {};
    if (("minhag" in route || "mode" in route) && !route.conversation && !route.chat && !route.a) return {};
    return route;
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

// The path for a route: its first path-able view, then the tail in a fixed
// order (conversation, community, mode, size, calendar day, account modal),
// else `/` (or the current page, when that's one the router doesn't own).
function pathFor(route, currentPath = "/") {
    const parts = [];
    const key = PATH_KEYS.find((k) => route[k]);
    if (key === "history") parts.push("history");
    else if (key === "siddur") parts.push("siddur", ...route.siddur.split("/"));
    else if (key) parts.push(PATH_SEGMENTS[key], encodeSegment(PATH_SLUGS[key] ? PATH_SLUGS[key].write(route[key]) : route[key]));
    if (route.conversation) parts.push("chat", encodeSegment(route.conversation));
    if (route.conversation || route.chat || route.a) {
        if (route.minhag && MINHAG_RE.test(route.minhag) && !RESERVED_WORDS.has(route.minhag.toLowerCase())) parts.push(route.minhag.toLowerCase());
        if (AI_MODES.includes(route.mode)) parts.push(route.mode);
    }
    if (route.conversation && CONVERSATION_SIZES.includes(route.cv) && route.cv !== DEFAULT_CONVERSATION_SIZE) parts.push(route.cv);
    if (route.date && DATE_RE.test(route.date)) parts.push("calendar", route.date);
    if (AUTH_PAGES.includes(route.auth)) parts.push(route.auth);
    if (parts.length) return `/${parts.join("/")}`;
    return isRouterPath(currentPath) ? "/" : currentPath;
}

function buildUrl(input, currentPath = "/") {
    const route = normalizeRoute({ ...input });
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

// The server renders each URL's <link rel="canonical"> (backend/page_meta.py);
// this keeps it true after an in-app navigation. A library view points at its
// own path (display keys and overlays aside), a private view -- one person's
// answer, their history, a shared answer, a conversation over the home page
// -- carries none.
const PRIVATE_VIEW_KEYS = Object.freeze(["chat", "a", "history"]);

// The page a URL is about: its view, else its calendar day, else home. The
// tail's overlays (a conversation, the account modal) sit on top of it.
function canonicalPath(route) {
    const view = PATH_KEYS.find((key) => route[key]);
    return pathFor(view ? { [view]: route[view] } : pickKeys(route, ["date"]));
}

function isPrivateRoute(route) {
    if (PRIVATE_VIEW_KEYS.some((key) => route[key])) return true;
    return Boolean(route.conversation) && canonicalPath(route) === "/";
}

function syncCanonical(route) {
    const doc = window.document;
    if (!doc) return;
    try {
        doc.querySelector(NOT_FOUND_ROBOTS)?.remove();
        const ogUrl = doc.querySelector('meta[property="og:url"]')?.getAttribute("content");
        const origin = ogUrl ? new URL(ogUrl).origin : window.location.origin;
        const path = window.location.pathname;
        if (!isRouterPath(path)) return;
        let link = doc.querySelector('link[rel="canonical"]');
        if (isPrivateRoute(route)) {
            link?.remove();
            return;
        }
        if (!link) {
            link = doc.createElement("link");
            link.setAttribute("rel", "canonical");
            doc.head.appendChild(link);
        }
        link.setAttribute("href", `${origin}${canonicalPath(route)}`);
    } catch (_) {
        // Non-critical: crawlers read the server-rendered tag.
    }
}

// The reader found this page's text doesn't exist (the server answers the
// next visit with a 404, backend/routes_spa_paths.py): keep this visit out of
// search results too. The next navigation lifts it.
const NOT_FOUND_ROBOTS = 'meta[name="robots"][data-not-found]';

function markNotFound() {
    const doc = window.document;
    if (!doc) return;
    try {
        doc.querySelector('link[rel="canonical"]')?.remove();
        if (doc.querySelector(NOT_FOUND_ROBOTS)) return;
        const meta = doc.createElement("meta");
        meta.setAttribute("name", "robots");
        meta.setAttribute("content", "noindex");
        meta.setAttribute("data-not-found", "");
        doc.head.appendChild(meta);
    } catch (_) {
        // Non-critical: the server's 404 covers the next visit.
    }
}

// Which route keys hold each overlay open.
const OVERLAY_ROUTE_KEYS = Object.freeze({ answer: ["chat", "a"], conversation: ["conversation"], calendar: ["date"], auth: ["auth"] });

// The state of the entry on screen: what this router last wrote, or what the
// last popstate brought back.
let shownState = null;

function currentState() {
    try {
        return window.history.state || null;
    } catch (_) {
        return null;
    }
}

function isOverlayEntry(state, kind = null) {
    return Boolean(state && state.pushedBy === "app" && state.overlay && (!kind || state.overlay === kind));
}

// The state for the entry about to be written. A push is one deeper than
// the entry it leaves and carries a marker only when it opens an overlay;
// a replace keeps the entry's depth and its marker while that overlay is
// still open (`overlay: null` drops it), and what the page saved on it
// (annotateEntry).
function nextState(route, push, overlay) {
    const current = currentState() || {};
    const seq = (Number(current.seq) || 0) + (push ? 1 : 0);
    const state = { shelahRoute: route, seq };
    if (!push && current.saved) state.saved = current.saved;
    const kind = push ? overlay : (overlay === null ? null : current.overlay);
    const marked = push ? Boolean(overlay) : isOverlayEntry(current);
    if (marked && kind && (OVERLAY_ROUTE_KEYS[kind] || []).some((key) => route[key])) {
        state.overlay = kind;
        state.pushedBy = "app";
    }
    return state;
}

// What the page keeps on the entry on screen (audit U-7: where the reader
// was), merged into its state's `saved` without changing its URL; handed
// back with the route when Back/Forward returns to it.
function annotateEntry(fields) {
    if (!fields) return;
    try {
        const current = currentState() || {};
        const state = { ...current, saved: { ...(current.saved || {}), ...fields } };
        window.history.replaceState(state, "");
        shownState = state;
    } catch (_) {
        // Non-critical history update failure.
    }
}

// A push to the URL already showing replaces it instead: re-opening the
// view you're on (a second click, a hydrate that re-renders the current
// route) must not stack a duplicate entry for Back to step through.
function writeHistory(route, replace, overlay) {
    try {
        const url = buildUrl(route, window.location.pathname);
        const same = url === `${window.location.pathname}${window.location.search}`;
        const push = !(replace || same);
        const state = nextState(route, push, overlay);
        window.history[push ? "pushState" : "replaceState"](state, "", url);
        shownState = state;
    } catch (_) {
        // Non-critical history update failure (e.g. sandboxed iframe).
    }
    syncCanonical(route);
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
//
// `overlay` ("answer" | "conversation" | "calendar" | "auth") marks a push that opens
// that overlay over the entry on screen, so closeOverlay() can step Back off
// it. On a replace it can only be `null`, which drops the marker.
function pushRoute(patch, { replace = false, exclusive = false, overlay = undefined } = {}) {
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
    writeHistory(next, replace, overlay);
    return next;
}

// Closes overlay `kind` in the URL. When the entry on screen is one the app
// pushed to open it, this is history.back(): the entry underneath comes back
// as it was, and Back afterwards doesn't re-open the overlay. The popstate
// that follows hands the route to the handler as usual, so callers close
// their UI and write nothing more. Otherwise (a cold-loaded or shared link)
// the overlay's keys are removed in place with `patch`. Returns "back" or
// "replace".
function closeOverlay(kind, patch) {
    if (isOverlayEntry(currentState(), kind)) {
        try {
            window.history.back();
            return "back";
        } catch (_) {
            // Fall through to the in-place removal.
        }
    }
    pushRoute(patch, { replace: true, overlay: null });
    return "replace";
}

// Navigate to an in-app URL (`/answer/<id>`, `/text/Genesis.1?lang=he`, an
// old `/?chat=<id>`): parsed like a cold load, pushed in canonical form, and
// handed to the route handler. Returns the route. A URL the router doesn't
// own (another origin, `/about`) is left alone and returns null.
function pushPath(url, { replace = false } = {}) {
    let parsed;
    try {
        // "https:" is an arbitrary parse anchor, never an actual request --
        // any scheme works here, and https avoids SonarCloud's blanket
        // "http is insecure" rule (javascript:S5332) on this literal.
        parsed = new URL(String(url), "https://router.invalid");
    } catch (_) {
        return null;
    }
    if (parsed.origin !== "https://router.invalid" && parsed.origin !== window.location.origin) return null;
    if (!isRouterPath(parsed.pathname)) return null;
    const next = routeFrom(parsed.pathname, parsed.search);
    writeHistory(next, replace);
    onRouteChange?.(next);
    return next;
}

// `keep` lists extra route keys to carry over (e.g. ["conversation", "cv"] to
// go home without closing an open conversation). Display keys are always
// kept -- going home doesn't undo the language/layout a link asked for.
// `patch` is applied on top (null removes a key), in the same single write.
function clearRoute({ replace = true, keep = [], patch = {} } = {}) {
    const next = pickKeys(readRoute(), [...DISPLAY_KEYS, ...keep]);
    Object.entries(patch || {}).forEach(([key, value]) => {
        if (value === null || value === undefined || value === "") delete next[key];
        else next[key] = String(value);
    });
    normalizeRoute(next);
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
        const state = { ...(window.history.state || {}), shelahRoute: route };
        window.history.replaceState(state, "", url);
        shownState = state;
    } catch (_) {
        // Non-critical history update failure.
    }
}

let onRouteChange = null;

// The `popstate` state object is trusted first (it's what we wrote), and
// re-parsing `location.search` is only a fallback for entries this router
// didn't create itself (e.g. a bookmarked/shared URL loaded directly).
//
// The handler's second argument says how the move went: `direction` is
// "back" or "forward" by the entries' `seq` (null when they're level). The
// entry a visit started on has no seq of its own; pushes count from it, so
// it reads as 0. `saved` is what the page kept on the entry
// (annotateEntry), or null; `left` is the route of the entry moved off.
//
// Scroll restoration is the page's: the browser's own runs at popstate,
// before an async view has rendered, and would fight it.
function installRouter(handler) {
    onRouteChange = typeof handler === "function" ? handler : null;
    try {
        if ("scrollRestoration" in window.history) window.history.scrollRestoration = "manual";
    } catch (_) {
        // Not settable here; the browser keeps restoring.
    }
    shownState = currentState();
    canonicalizeUrl();
    window.addEventListener("popstate", (event) => {
        const state = event.state || null;
        const route = (state && state.shelahRoute) || readRoute();
        const from = Number(shownState?.seq) || 0;
        const to = Number(state?.seq) || 0;
        const left = shownState?.shelahRoute || null;
        shownState = state;
        const direction = to === from ? null : (to < from ? "back" : "forward");
        syncCanonical(route);
        onRouteChange?.(route, { direction, saved: state?.saved || null, left });
    });
}

window.ShelahRouter = { readRoute, pushRoute, pushPath, clearRoute, closeOverlay, annotateEntry, installRouter, markNotFound, routeUrl: buildUrl, parsePath, VIEW_KEYS, DISPLAY_KEYS, AI_KEYS, AI_MODES, AUTH_PAGES, CONVERSATION_SIZES, READER_LAYOUTS };

export { readRoute, pushRoute, pushPath, clearRoute, closeOverlay, annotateEntry, installRouter, buildUrl as routeUrl, parsePath, VIEW_KEYS, DISPLAY_KEYS, AI_KEYS, AI_MODES, AUTH_PAGES, CONVERSATION_SIZES, READER_LAYOUTS };
