// Stored AI answers outside the answer panel itself:
//
// 1. The /history page (router.js `history` key): everything a signed-in
//    reader has asked, newest first, searchable by question, paged by
//    GET /api/user/history?limit=&cursor=&q= (backend/routes_user.py --
//    keyset on created_at,id, so a new answer arriving mid-scroll never
//    shifts or repeats a page). Each row links to /answer/<id>; delete asks
//    for an inline confirm first.
// 2. Shelf 'ask' entries (Recent / Bookmarks). They used to hold the
//    question text and re-ran /ask when opened; they now hold the answer's
//    ask_history id (label = the question) and reopen the stored answer.
//    promoteAskEntries() rewrites the entries for a question once its
//    answer is stored; resolveShelfAsk() opens an entry, matching a legacy
//    question-text entry to the stored answer when there is one and
//    re-asking otherwise.
//
// index.html's displayAskHistoryPage() owns the reader chrome (title,
// sidebar, route) and hands createHistoryPage() the container plus the
// app's own helpers (auth, fetch, t(), icons), so this module never
// reaches for a classic-script global.

const HISTORY_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export const PAGE_SIZE = 20;
export const SEARCH_DEBOUNCE_MS = 300;
// The search box's cap; the API trims queries to the same length.
export const MAX_QUERY_CHARS = 200;
const SKELETON_ROWS = 6;
const SHELF_BUCKETS = ["recent", "bookmarks"];

export function isHistoryId(value) {
    return HISTORY_ID_RE.test(String(value || "").trim());
}

function normalizeQuestion(text) {
    return String(text || "").replace(/\s+/g, " ").trim().toLowerCase();
}

export function sameQuestion(a, b) {
    const left = normalizeQuestion(a);
    return Boolean(left) && left === normalizeQuestion(b);
}

// The shelf with this question's 'ask' entries pointing at its stored answer
// (`id`), or null when there was nothing to rewrite. Entries already keyed
// by an id are left alone; a duplicate the rewrite creates is dropped.
export function promoteAskEntries(shelf, question, id) {
    if (!shelf || !isHistoryId(id) || !normalizeQuestion(question)) return null;
    const label = String(question).trim();
    const key = `ask:${id}`;
    let changed = false;
    const next = { ...shelf };
    for (const bucket of SHELF_BUCKETS) {
        if (!Array.isArray(shelf[bucket])) continue;
        let seen = false;
        next[bucket] = [];
        for (const item of shelf[bucket]) {
            let entry = item;
            if (item?.type === "ask" && !isHistoryId(item.value) && sameQuestion(item.value, question)) {
                entry = { ...item, value: id, label: String(item.label || "").trim() || label };
                changed = true;
            }
            if (entry?.type === "ask" && `ask:${entry.value}` === key) {
                if (seen) {
                    changed = true;
                    continue;
                }
                seen = true;
            }
            next[bucket].push(entry);
        }
    }
    return changed ? next : null;
}

// What opening a shelf 'ask' entry should do:
//   { item }              -- a stored answer the reader can open
//   { item, migrateTo }   -- the same, found for a legacy question-text
//                            entry; the caller rewrites the entry to this id
//   { reask: question }   -- nothing stored (signed out, deleted, or never
//                            stored): ask the question again
//   { reask, staleId }    -- the entry's answer is gone (fetchEntry found
//                            nothing): the caller drops the dead entry
// `fetchEntry(id)` resolves an answer, or null when there is none;
// `searchHistory(q)` resolves a history page ({ items }). Either may throw
// (a network or server error): that re-asks, and keeps the entry.
export async function resolveShelfAsk(entry, { signedIn, fetchEntry, searchHistory }) {
    const value = String(entry?.value || "").trim();
    const byId = isHistoryId(value);
    const question = byId ? String(entry?.label || "").trim() : (value || String(entry?.label || "").trim());
    if (signedIn) {
        try {
            if (byId) {
                const item = await fetchEntry(value);
                if (item?.answer) return { item };
                return { reask: question, staleId: value };
            } else if (question) {
                const page = await searchHistory(question);
                const match = (Array.isArray(page?.items) ? page.items : [])
                    .find((row) => isHistoryId(row?.id) && row?.answer && sameQuestion(row.question, question));
                if (match) return { item: match, migrateTo: match.id };
            }
        } catch (_) {
            // Fall through to re-asking.
        }
    }
    return { reask: question };
}

// ── /history page ──────────────────────────────────────────────────────

export function historyQueryString({ q = "", cursor = "", limit = PAGE_SIZE } = {}) {
    const params = new URLSearchParams({ limit: String(limit) });
    const query = String(q || "").trim();
    if (query) params.set("q", query);
    if (cursor) params.set("cursor", cursor);
    return params.toString();
}

function skeletonHtml() {
    const row = `
        <li class="history-row history-row--skeleton" aria-hidden="true">
            <span class="sk-line history-skel history-skel--q"></span>
            <span class="sk-line history-skel history-skel--meta"></span>
        </li>`;
    return row.repeat(SKELETON_ROWS);
}

// Markup for the page frame; the list region is re-rendered on its own so
// the search box keeps focus and its caret while results update.
export function shellHtml({ t, escapeHtml, icons = {} }) {
    return `
        <section class="history-page not-prose" aria-labelledby="historyPageHeading">
            <h2 id="historyPageHeading" class="sr-only">${escapeHtml(t("Your questions", "השאלות שלך"))}</h2>
            <form class="history-search" role="search" data-history-search>
                <label class="sr-only" for="historySearchInput">${escapeHtml(t("Search your questions", "חיפוש בשאלות שלך"))}</label>
                <span class="history-search-icon" aria-hidden="true"><svg viewBox="0 0 256 256" fill="currentColor" aria-hidden="true">${icons.search || ""}</svg></span>
                <input id="historySearchInput" class="history-search-input" type="search" autocomplete="off"
                    maxlength="${MAX_QUERY_CHARS}" enterkeyhint="search"
                    placeholder="${escapeHtml(t("Search your questions", "חיפוש בשאלות שלך"))}">
            </form>
            <div class="history-results" data-history-results aria-live="off"></div>
            <div class="history-more" data-history-more></div>
            <p class="sr-only" role="status" aria-live="polite" data-history-status></p>
        </section>`;
}

function rowHtml(item, state, h) {
    const { t, escapeHtml, formatDate, answerHref, icons = {} } = h;
    const id = String(item.id);
    const question = String(item.question || "").trim() || t("Untitled question", "שאלה ללא כותרת");
    const date = item.created_at ? formatDate(item.created_at) : "";
    const confirming = state.confirmId === id;
    const deleting = state.deleting.has(id);
    const safeId = escapeHtml(id);
    const actions = confirming
        ? `<div class="history-row-confirm" role="group" aria-label="${escapeHtml(t("Delete this answer?", "למחוק תשובה זו?"))}">
                <span class="history-row-confirm-text" aria-hidden="true">${escapeHtml(t("Delete?", "למחוק?"))}</span>
                <button type="button" class="history-btn history-btn--danger" data-action="confirm-delete" data-id="${safeId}"
                    ${deleting ? 'disabled aria-busy="true"' : ""}>${escapeHtml(t("Delete", "מחק"))}</button>
                <button type="button" class="history-btn" data-action="cancel-delete" data-id="${safeId}"
                    ${deleting ? "disabled" : ""}>${escapeHtml(t("Cancel", "ביטול"))}</button>
           </div>`
        : `<button type="button" class="history-row-del" data-action="delete" data-id="${safeId}"
                aria-label="${escapeHtml(`${t("Delete", "מחק")}: ${question}`)}">
                <svg viewBox="0 0 256 256" fill="currentColor" aria-hidden="true">${icons.delete || ""}</svg>
           </button>`;
    return `
        <li class="history-row${confirming ? " history-row--confirming" : ""}" data-id="${safeId}">
            <a class="history-row-open" href="${escapeHtml(answerHref(id))}" data-action="open" data-id="${safeId}">
                <span class="history-row-q">${escapeHtml(question)}</span>
                ${date ? `<time class="history-row-date" datetime="${escapeHtml(String(item.created_at))}">${escapeHtml(date)}</time>` : ""}
            </a>
            ${actions}
        </li>`;
}

function messageHtml(text, escapeHtml, action = null) {
    return `
        <div class="history-message">
            <p>${escapeHtml(text)}</p>
            ${action ? `<button type="button" class="history-btn history-btn--primary" data-action="${action.name}">${escapeHtml(action.label)}</button>` : ""}
        </div>`;
}

// The list region for a page state (pure, so tests can assert on it).
export function resultsHtml(state, h) {
    const { t, escapeHtml } = h;
    if (state.status === "pending" || (state.status === "loading" && !state.items.length)) {
        return `<ul class="history-list" aria-busy="true">${skeletonHtml()}</ul>`;
    }
    if (state.status === "unavailable") {
        return messageHtml(t("Question history isn't available here.", "היסטוריית השאלות אינה זמינה כאן."), escapeHtml);
    }
    if (state.status === "signed-out") {
        return messageHtml(
            t("Sign in to see every question you've asked, and pick up any answer again.",
                "יש להתחבר כדי לראות את כל השאלות ששאלת ולחזור לכל תשובה."),
            escapeHtml,
            { name: "sign-in", label: t("Sign in", "התחברות") },
        );
    }
    if (state.status === "error" && !state.items.length) {
        return messageHtml(t("Couldn't load your questions.", "לא ניתן היה לטעון את השאלות שלך."), escapeHtml,
            { name: "retry", label: t("Try again", "נסה שוב") });
    }
    if (!state.items.length) {
        return state.q
            ? messageHtml(t(`No questions match “${state.q}”.`, `אין שאלות שתואמות ל“${state.q}”.`), escapeHtml)
            : messageHtml(t("No saved answers yet. Questions you ask while signed in appear here.",
                "אין עדיין תשובות שמורות. שאלות שתשאל/י כשאת/ה מחובר/ת יופיעו כאן."), escapeHtml);
    }
    return `<ul class="history-list" aria-label="${escapeHtml(t("Your questions", "השאלות שלך"))}">${
        state.items.map((item) => rowHtml(item, state, h)).join("")}</ul>`;
}

export function moreHtml(state, { t, escapeHtml }) {
    if (!state.items.length) return "";
    if (state.status === "error") {
        return `<button type="button" class="history-btn" data-action="load-more">${escapeHtml(t("Couldn't load more — try again", "הטעינה נכשלה — נסה שוב"))}</button>`;
    }
    if (!state.nextCursor) return "";
    const busy = state.loadingMore;
    return `<button type="button" class="history-btn" data-action="load-more"${busy ? ' aria-busy="true" disabled' : ""}>${
        escapeHtml(busy ? t("Loading…", "טוען…") : t("Load more", "טען עוד"))}</button>`;
}

// deps:
//   authState()            'signed-in' | 'signed-out' | 'pending' | 'unavailable'
//   fetchPage({q, cursor}) resolves { items, next_cursor }; rejects on failure
//   deleteEntry(id)        resolves true once deleted
//   openEntry(item)        opens a stored answer (index.html openStoredAskAnswer)
//   signIn()               opens Clerk's sign-in
//   onDeleted(id)          optional: refresh other views of the history
//   t, escapeHtml, formatDate(iso), answerHref(id), icons { search, delete }
//   setTimeout, clearTimeout (injectable for tests)
export function createHistoryPage(root, deps) {
    const setTimer = deps.setTimeout || setTimeout;
    const clearTimer = deps.clearTimeout || clearTimeout;
    const state = {
        status: "pending",
        items: [],
        nextCursor: null,
        q: "",
        loadingMore: false,
        confirmId: null,
        deleting: new Set(),
    };
    let requestSeq = 0;
    let searchTimer = null;
    let destroyed = false;

    root.innerHTML = shellHtml(deps);
    const input = root.querySelector("#historySearchInput");
    const results = root.querySelector("[data-history-results]");
    const more = root.querySelector("[data-history-more]");
    const status = root.querySelector("[data-history-status]");

    function render() {
        if (destroyed) return;
        results.innerHTML = resultsHtml(state, deps);
        more.innerHTML = moreHtml(state, deps);
        // Nothing to search until there is a list to search.
        input.disabled = state.status === "signed-out" || state.status === "unavailable";
    }

    function announce(text) {
        status.textContent = "";
        status.textContent = text;
    }

    function focusIn(selector) {
        const el = root.querySelector(selector);
        if (el?.focus) el.focus();
    }

    async function load({ append = false } = {}) {
        const seq = ++requestSeq;
        const q = state.q;
        if (append) state.loadingMore = true;
        else {
            state.status = "loading";
            state.items = [];
            state.nextCursor = null;
            state.confirmId = null;
        }
        render();
        try {
            const page = await deps.fetchPage({ q, cursor: append ? state.nextCursor : "" });
            if (seq !== requestSeq || destroyed) return;
            const incoming = Array.isArray(page?.items) ? page.items.filter((item) => isHistoryId(item?.id)) : [];
            const known = new Set(state.items.map((item) => item.id));
            state.items = append ? state.items.concat(incoming.filter((item) => !known.has(item.id))) : incoming;
            state.nextCursor = page?.next_cursor || null;
            state.status = "ready";
            state.loadingMore = false;
            render();
            if (append) {
                announce(deps.t(`${incoming.length} more loaded.`, `נטענו עוד ${incoming.length}.`));
            } else if (q) {
                announce(state.items.length
                    ? deps.t(`${state.items.length}${state.nextCursor ? "+" : ""} matching questions.`,
                        `${state.items.length}${state.nextCursor ? "+" : ""} שאלות תואמות.`)
                    : deps.t("No matching questions.", "אין שאלות תואמות."));
            }
        } catch (_) {
            if (seq !== requestSeq || destroyed) return;
            state.status = "error";
            state.loadingMore = false;
            render();
            announce(deps.t("Couldn't load your questions.", "לא ניתן היה לטעון את השאלות שלך."));
        }
    }

    // Re-read the auth state and (re)load: on open, and whenever sign-in
    // changes while the page is showing.
    function refresh() {
        if (destroyed) return;
        const auth = deps.authState();
        if (auth !== "signed-in") {
            requestSeq++;
            state.status = auth === "pending" || auth === "unavailable" ? auth : "signed-out";
            state.items = [];
            state.nextCursor = null;
            render();
            return;
        }
        void load();
    }

    function onInput() {
        const next = String(input.value || "").replace(/\s+/g, " ").trim().slice(0, MAX_QUERY_CHARS);
        if (searchTimer) clearTimer(searchTimer);
        searchTimer = setTimer(() => {
            searchTimer = null;
            if (next === state.q && state.status !== "error") return;
            state.q = next;
            void load();
        }, SEARCH_DEBOUNCE_MS);
    }

    function onSubmit(event) {
        event.preventDefault();
        if (searchTimer) clearTimer(searchTimer);
        searchTimer = null;
        state.q = String(input.value || "").replace(/\s+/g, " ").trim().slice(0, MAX_QUERY_CHARS);
        void load();
    }

    async function confirmDelete(id) {
        if (state.deleting.has(id)) return;
        const index = state.items.findIndex((item) => item.id === id);
        state.deleting.add(id);
        render();
        let ok = false;
        try {
            ok = Boolean(await deps.deleteEntry(id));
        } catch (_) {
            ok = false;
        }
        state.deleting.delete(id);
        if (destroyed) return;
        if (!ok) {
            render();
            announce(deps.t("Couldn't delete that answer. Try again.", "לא ניתן היה למחוק את התשובה. נסה שוב."));
            focusIn(`[data-action="confirm-delete"][data-id="${id}"]`);
            return;
        }
        state.items = state.items.filter((item) => item.id !== id);
        state.confirmId = null;
        render();
        announce(deps.t("Answer deleted.", "התשובה נמחקה."));
        deps.onDeleted?.(id);
        // Keep the keyboard where the row was: the next row, else the
        // previous one, else the search box.
        const neighbour = state.items[Math.min(index, state.items.length - 1)];
        if (neighbour) focusIn(`[data-action="open"][data-id="${neighbour.id}"]`);
        else input.focus?.();
    }

    function onClick(event) {
        const target = event.target?.closest?.("[data-action]");
        if (!target || !root.contains?.(target)) return;
        const action = target.getAttribute("data-action");
        const id = target.getAttribute("data-id");
        if (action === "open") {
            // A modified click (new tab/window) keeps the link's own behaviour.
            if (event.button > 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
            const item = state.items.find((row) => row.id === id);
            if (!item) return;
            event.preventDefault();
            deps.openEntry(item);
        } else if (action === "delete") {
            state.confirmId = id;
            render();
            focusIn(`[data-action="cancel-delete"][data-id="${id}"]`);
        } else if (action === "cancel-delete") {
            state.confirmId = null;
            render();
            focusIn(`[data-action="delete"][data-id="${id}"]`);
        } else if (action === "confirm-delete") {
            void confirmDelete(id);
        } else if (action === "load-more") {
            if (!state.loadingMore) void load({ append: state.items.length > 0 });
        } else if (action === "retry") {
            void load();
        } else if (action === "sign-in") {
            deps.signIn();
        }
    }

    function onKeydown(event) {
        if (event.key !== "Escape" || !state.confirmId) return;
        const id = state.confirmId;
        event.preventDefault?.();
        state.confirmId = null;
        render();
        focusIn(`[data-action="delete"][data-id="${id}"]`);
    }

    const form = root.querySelector("[data-history-search]");
    input.addEventListener("input", onInput);
    form.addEventListener("submit", onSubmit);
    root.addEventListener("click", onClick);
    root.addEventListener("keydown", onKeydown);

    refresh();

    return {
        refresh,
        state,
        destroy() {
            destroyed = true;
            requestSeq++;
            if (searchTimer) clearTimer(searchTimer);
            input.removeEventListener("input", onInput);
            form.removeEventListener("submit", onSubmit);
            root.removeEventListener("click", onClick);
            root.removeEventListener("keydown", onKeydown);
        },
    };
}
