// Public share links for stored AI answers (deep-link Phase 4) and for whole
// conversations.
//
// The owner's Share button (static/js/answer-link.js) copies a public link,
// `/a/<share token>`, that anyone can open signed out. This module owns:
//
//   * the API clients for backend/routes_answer_share.py
//     (GET/POST/DELETE /api/user/history/<id>/share) and
//     backend/routes_conversation_share.py
//     (GET/POST/DELETE /api/conversations/<id>/share) -- one link shape for
//     both, so one client class with a different base path;
//   * one share-state store per kind, shared by every placement showing the
//     same answer (or conversation);
//   * the "Shared · anyone with the link can view" line + "Stop sharing"
//     control inside each answer placement (`.answer-share-state`).
//
// Sharing an answer is idempotent server-side, so copying twice never
// changes the link. Sharing a conversation keeps the token too, but moves its
// snapshot forward to the newest turn each time (`refreshOnShare`). Stopping
// sharing kills the link for good; sharing again mints a new one. Until the
// operator runs scripts/sql/migrate_ask_history_share.sql the answer API
// answers 503 share_unavailable, and the answer's Share button falls back to
// the owner-only `/answer/<id>` link with a visible note saying so.

import { answerLinkUrl } from "./answer-link.js";

export const SHARE_TOKEN_RE = /^[A-Za-z0-9_-]{16,64}$/;

export function shareLinkUrl(token, origin) {
    return `${origin}/a/${encodeURIComponent(token)}`;
}

export class ShareUnavailableError extends Error {
    constructor() {
        super("share_unavailable");
        this.name = "ShareUnavailableError";
    }
}

// A conversation with no finished turn yet (POST .../share -> 409).
export class NothingToShareError extends Error {
    constructor() {
        super("empty_conversation");
        this.name = "NothingToShareError";
    }
}

export const ANSWER_SHARE_PATH = "/api/user/history";
export const CONVERSATION_SHARE_PATH = "/api/conversations";

function defaultTranslate(en, he) {
    return typeof window.t === "function" ? window.t(en, he) : en;
}

async function defaultHeaders() {
    if (typeof window.authHeaders !== "function") return {};
    try {
        return await window.authHeaders({});
    } catch (_) {
        return {};
    }
}

function normalizeState(body) {
    const token = String(body?.share_token || "");
    return body?.shared && SHARE_TOKEN_RE.test(token) ? { shared: true, token } : { shared: false };
}

export function createShareApi({
    fetchImpl = (...args) => fetch(...args),
    getHeaders = defaultHeaders,
    basePath = ANSWER_SHARE_PATH,
} = {}) {
    async function call(method, id) {
        const headers = await getHeaders();
        const resp = await fetchImpl(`${basePath}/${encodeURIComponent(id)}/share`, {
            method,
            headers,
            cache: "no-store",
        });
        let body = null;
        try {
            body = await resp.json();
        } catch (_) {
            body = null;
        }
        if (resp.status === 503 && body?.code === "share_unavailable") throw new ShareUnavailableError();
        if (resp.status === 409 && body?.code === "empty_conversation") throw new NothingToShareError();
        if (!resp.ok) throw new Error(`share ${method} failed: ${resp.status}`);
        return normalizeState(body);
    }
    return {
        get: (id) => call("GET", id),
        create: (id) => call("POST", id),
        revoke: (id) => call("DELETE", id),
    };
}

// States: { shared: true, token } | { shared: false } |
// { shared: false, unavailable: true } (migration not run yet).
//
// `privateFallback` is for answers only: an owner-only /answer/<id> link to
// copy while public links aren't set up. A conversation has none (its routes
// never answer 503), so it passes false and the error surfaces.
// `refreshOnShare` posts again even when the link is already live, which is
// how a conversation's snapshot moves forward to its newest turn.
export function createAnswerShare({
    api = createShareApi(),
    getOrigin = () => window.location.origin,
    translate = defaultTranslate,
    privateFallback: allowPrivateFallback = true,
    refreshOnShare = false,
} = {}) {
    const states = new Map();
    const loading = new Map();
    const listeners = new Set();

    function set(id, state) {
        states.set(id, state);
        listeners.forEach((fn) => fn(id, state));
        return state;
    }

    function privateFallback(id) {
        return {
            url: answerLinkUrl(id, getOrigin()),
            note: translate(
                "Copied a private link: only you can open it while signed in. Public links aren't set up yet.",
                "הועתק קישור פרטי: רק את/ה יכול/ה לפתוח אותו כשאת/ה מחובר/ת. קישורים ציבוריים עדיין לא הוגדרו.",
            ),
        };
    }

    function load(id) {
        if (!id) return Promise.resolve(null);
        if (loading.has(id)) return loading.get(id);
        const request = api.get(id)
            .then((state) => set(id, state))
            .catch((err) => (err instanceof ShareUnavailableError ? set(id, { shared: false, unavailable: true }) : states.get(id) || null))
            .finally(() => loading.delete(id));
        loading.set(id, request);
        return request;
    }

    // answer-link.js's getLinkUrl hook.
    async function linkFor(id) {
        const known = states.get(id);
        if (known?.shared && !refreshOnShare) return shareLinkUrl(known.token, getOrigin());
        if (known?.unavailable && allowPrivateFallback) return privateFallback(id);
        let state;
        try {
            state = set(id, await api.create(id));
        } catch (err) {
            if (!allowPrivateFallback || !(err instanceof ShareUnavailableError)) throw err;
            set(id, { shared: false, unavailable: true });
            return privateFallback(id);
        }
        if (!state.shared) throw new Error("share was not created");
        return shareLinkUrl(state.token, getOrigin());
    }

    async function revoke(id) {
        await api.revoke(id);
        return set(id, { shared: false });
    }

    return {
        peek: (id) => states.get(id) || null,
        load,
        linkFor,
        revoke,
        subscribe(fn) {
            listeners.add(fn);
            return () => listeners.delete(fn);
        },
    };
}

// Wires one placement's `.answer-share-state` line. `show(id)` renders the
// cached state at once (no flash when the modal and the article view show
// the same answer) and refreshes it from the server; `hide()` clears it.
// Status messages go to the placement's existing `.answer-link-status` live
// region, which stays in the DOM when the Shared line hides.
export function installShareState(root, share, { translate = defaultTranslate } = {}) {
    const line = root?.querySelector(".answer-share-state");
    if (!line) return { show() {}, hide() {} };
    const revokeBtn = line.querySelector(".answer-share-revoke");
    const status = root.querySelector(".answer-link-status");
    const copyBtn = root.querySelector(".answer-link-btn");
    let currentId = null;

    function render(state) {
        const shared = Boolean(currentId && state?.shared);
        line.classList.toggle("hidden", !shared);
        if (!shared) revokeBtn.disabled = false;
    }

    function announce(text, visible) {
        if (!status) return;
        status.textContent = text;
        status.classList.toggle("is-visible", visible);
    }

    share.subscribe((id, state) => {
        if (id === currentId) render(state);
    });

    revokeBtn.addEventListener("click", async () => {
        const id = currentId;
        if (!id || revokeBtn.disabled) return;
        revokeBtn.disabled = true;
        revokeBtn.setAttribute("aria-busy", "true");
        try {
            await share.revoke(id);
            if (id === currentId) {
                // The Shared line (and this button) just hid: keep keyboard
                // focus in the control instead of dropping it to <body>.
                copyBtn?.focus({ preventScroll: true });
                announce(translate("Sharing stopped. The old link no longer works.", "השיתוף הופסק. הקישור הישן כבר לא עובד."), true);
            }
        } catch (_) {
            if (id === currentId) {
                revokeBtn.disabled = false;
                announce(translate("Couldn't stop sharing. Try again.", "לא ניתן היה להפסיק את השיתוף. נסה/י שוב."), true);
            }
        } finally {
            revokeBtn.removeAttribute("aria-busy");
        }
    });

    return {
        show(id) {
            currentId = id || null;
            revokeBtn.disabled = false;
            render(currentId ? share.peek(currentId) : null);
            if (currentId) void share.load(currentId);
        },
        hide() {
            currentId = null;
            render(null);
        },
    };
}
