// View-model for the multi-turn conversation UI: one thread's header +
// transcript, the "sources consulted" state behind the composer's
// <details class="sources-disclosure">, and the sidebar list. Owns no DOM --
// every surface (overlay, mini widget, full page, mobile sheet/PiP) renders
// from getState() and calls the methods below, so all of them show the same
// thread because they read the same store.
//
// Minhag lock (backend/routes_conversations.py): a conversation's `minhag` is
// fixed when the row is created and /ask reads it from the row, never from
// the request. The store creates the row lazily, on the FIRST send, so the
// community stays an editable draft (draftMinhag) until the thread has its
// first question, and is locked (minhagLocked) from then on. Switching
// community on a locked thread means startNew({ minhag }), never an edit.
//
// Async safety: every async method captures `generation` up front and drops
// its result if the user has since opened/started another thread, so a slow
// answer can never land in the wrong transcript.

import * as defaultApi from "./conversation-api.js";
import { applyProgressEvent, createProgress } from "./ask-progress.js";

const { ERROR_CODES } = defaultApi;

// Design's Standard/Deep segmented control -> backend ANSWER_MODES
// (backend/helpers.py). Any other valid backend mode passes through as-is.
export const ANSWER_MODE_BY_CHOICE = Object.freeze({ standard: "balanced", deep: "sources" });

export const MESSAGE_STATUS = Object.freeze({
    COMPLETE: "complete",
    ERROR: "error",             // server stored a failed answer turn (retryable)
    PENDING: "pending",         // local optimistic turn awaiting the server
    FAILED: "failed",           // local user turn the server never saved
    INCOMPLETE: "incomplete",   // local answer slot: question saved, answer unknown yet
});

export const SOURCES_PHASE = Object.freeze({
    IDLE: "idle",
    SEARCHING: "searching",     // request in flight; real citations not known yet
    DONE: "done",               // citations = the answer's stored citations
    UNAVAILABLE: "unavailable", // the answer failed, so nothing was consulted
});

const LOCAL_ID_PREFIX = "local-";

// Only https links and same-origin absolute paths become clickable; anything
// else (javascript:, data:, protocol-relative) is dropped to null.
export function safeCitationUrl(value) {
    const url = String(value || "").trim();
    if (!url) return null;
    if (url.startsWith("/") && !url.startsWith("//")) return url;
    try {
        return new URL(url).protocol === "https:" ? url : null;
    } catch (_err) {
        return null;
    }
}

export function normalizeCitations(citations) {
    return (Array.isArray(citations) ? citations : [])
        .filter((c) => c && typeof c === "object")
        .map((c, i) => ({
            id: c.id ?? null,
            ordinal: Number.isFinite(Number(c.ordinal)) ? Number(c.ordinal) : i,
            ref: String(c.source_ref || "").trim(),
            excerptEn: String(c.excerpt_en || "").trim(),
            excerptHe: String(c.excerpt_he || "").trim(),
            url: safeCitationUrl(c.url),
        }))
        .filter((c) => c.ref || c.excerptEn || c.excerptHe)
        .sort((a, b) => a.ordinal - b.ordinal);
}

export function normalizeMessage(raw) {
    return {
        id: raw?.id ?? null,
        role: raw?.role === "assistant" ? "assistant" : "user",
        content: String(raw?.content || ""),
        status: String(raw?.status || MESSAGE_STATUS.COMPLETE),
        createdAt: raw?.created_at || null,
        citations: normalizeCitations(raw?.citations),
    };
}

function normalizeHeader(raw) {
    if (!raw || !raw.id) return null;
    return {
        id: String(raw.id),
        title: String(raw.title || ""),
        titleIsCustom: Boolean(raw.title_is_custom),
        minhag: raw.minhag || null,
        pinnedAt: raw.pinned_at || null,
        createdAt: raw.created_at || null,
        updatedAt: raw.updated_at || null,
    };
}

// The lock chip / "Answers follow X practice" label. A null/"All" minhag is
// still a lock (the thread answers across all communities), so it reads "All".
export function minhagLabel(minhag) {
    const value = String(minhag || "").trim();
    return value && value.toLowerCase() !== "all" ? value : "All";
}

// Sidebar ordering, matching list_conversations(): pinned first (newest pin
// first), then most-recently-updated.
function sortListItems(items) {
    const time = (v) => (v ? Date.parse(v) || 0 : 0);
    return [...items].sort((a, b) => {
        if (Boolean(a.pinnedAt) !== Boolean(b.pinnedAt)) return a.pinnedAt ? -1 : 1;
        if (a.pinnedAt && b.pinnedAt) return time(b.pinnedAt) - time(a.pinnedAt);
        return time(b.updatedAt) - time(a.updatedAt);
    });
}

function initialThreadState(draftMinhag) {
    return {
        conversation: null,
        draftMinhag: draftMinhag || "All",
        minhagLocked: false,
        messages: [],
        loadStatus: "ready",
        loadError: null,
        sending: false,
        // Live steps of the answer being prepared (ask-progress.js); reset on
        // every send and only meaningful while `sending`.
        progress: createProgress(),
        sources: { phase: SOURCES_PHASE.IDLE, citations: [], messageId: null },
        lastError: null,
        // A search-bar answer (/ask, saved to ask_history) shown as the
        // thread's first turn before any conversation row exists:
        // { historyId, isPublic, request }. The first follow-up turns it into
        // a conversation (ensureConversation). A shared conversation read from
        // its /a/<token> link is the same view, with the chat's `title`. null
        // for ordinary threads.
        answerView: null,
    };
}

// A search-bar answer's "Ref — note" strings (ai_cited_sources) as citations,
// the same way the server seeds them when the answer becomes a conversation
// (routes_conversations._seed_citations): de-duplicated by ref, capped at six.
const CITED_SOURCE_RE = /^(.*?)\s[\u2013\u2014]\s(.*)$/;
const CITED_SOURCE_LIMIT = 6;

export function answerCitations(data) {
    const cited = Array.isArray(data?.ai_cited_sources) ? data.ai_cited_sources : [];
    const seen = new Set();
    const citations = [];
    for (const raw of cited) {
        const text = String(raw || "").trim();
        const match = text.match(CITED_SOURCE_RE);
        const ref = (match ? match[1] : text).trim();
        if (!ref || seen.has(ref.toLowerCase())) continue;
        seen.add(ref.toLowerCase());
        citations.push({ id: null, ordinal: citations.length, ref, excerptEn: match ? match[2].trim() : "", excerptHe: "", url: null });
        if (citations.length >= CITED_SOURCE_LIMIT) break;
    }
    return citations;
}

// The one-shot /ask client (ai-service.js askAi) throws plain Errors carrying
// .status / .code / .name; map them onto the codes the notices understand.
export const SEARCH_ERROR_CODES = Object.freeze({ TURNSTILE: "turnstile_required", TIMEOUT: "timeout" });

function searchErrorCode(error) {
    if (error?.code === SEARCH_ERROR_CODES.TURNSTILE) return SEARCH_ERROR_CODES.TURNSTILE;
    if (error?.name === "AbortError") return SEARCH_ERROR_CODES.TIMEOUT;
    switch (error?.status) {
        case 401: return ERROR_CODES.UNAUTHORIZED;
        case 429: return ERROR_CODES.RATE_LIMITED;
        case undefined: case null: return ERROR_CODES.NETWORK;
        default: return ERROR_CODES.SERVER;
    }
}

function isServerId(id) {
    return Boolean(id) && !String(id).startsWith(LOCAL_ID_PREFIX);
}

export function createConversationStore({ api = defaultApi, getPrefs = () => ({}), askAnswer = null } = {}) {
    let state = {
        ...initialThreadState(getPrefs()?.community),
        list: { status: "idle", items: [], error: null },
        // The most recent successful remove(), kept so undoRemove() can bring
        // it back: { id, header, wasOpen }. Survives thread switches.
        lastDeleted: null,
    };
    let generation = 0;
    let localCounter = 0;
    const listeners = new Set();

    function setState(patch) {
        state = { ...state, ...patch };
        for (const listener of listeners) {
            try {
                listener(state);
            } catch (_err) {
                // One broken surface must not stop the others from rendering.
            }
        }
    }

    function nextLocalId() {
        localCounter += 1;
        return `${LOCAL_ID_PREFIX}${localCounter}`;
    }

    function errorInfo(error) {
        return {
            code: error?.code || ERROR_CODES.NETWORK,
            message: String(error?.message || "Something went wrong"),
            retryAfter: error?.retryAfter ?? null,
        };
    }

    // The onProgress hook handed to the ask clients. Steps from a request the
    // user has since moved away from (another thread opened) are dropped, like
    // its result would be.
    function progressSink(myGeneration) {
        return (event) => {
            if (myGeneration !== generation) return;
            const progress = applyProgressEvent(state.progress, event);
            if (progress !== state.progress) setState({ progress });
        };
    }

    function upsertListItem(header) {
        if (!header) return;
        const items = state.list.items.filter((item) => item.id !== header.id);
        setState({ list: { ...state.list, items: sortListItems([header, ...items]) } });
    }

    function removeListItem(id) {
        setState({ list: { ...state.list, items: state.list.items.filter((item) => item.id !== id) } });
    }

    // ── thread lifecycle ────────────────────────────────────────────────

    function startNew({ minhag } = {}) {
        generation += 1;
        setState(initialThreadState(minhag ?? getPrefs()?.community));
    }

    async function open(id) {
        const conversationId = String(id || "").trim();
        if (!conversationId) return false;
        const myGeneration = ++generation;
        setState({
            ...initialThreadState(state.draftMinhag),
            conversation: state.conversation?.id === conversationId ? state.conversation : null,
            loadStatus: "loading",
        });
        try {
            const thread = await api.getConversation(conversationId);
            if (myGeneration !== generation) return false;
            const conversation = normalizeHeader(thread);
            const messages = (thread?.messages || []).map(normalizeMessage);
            const lastAnswer = [...messages].reverse().find((m) => m.role === "assistant");
            setState({
                conversation,
                minhagLocked: true,
                messages,
                loadStatus: "ready",
                sources: lastAnswer
                    ? { phase: SOURCES_PHASE.DONE, citations: lastAnswer.citations, messageId: lastAnswer.id }
                    : { phase: SOURCES_PHASE.IDLE, citations: [], messageId: null },
            });
            return true;
        } catch (error) {
            if (myGeneration !== generation) return false;
            setState({ loadStatus: "error", loadError: errorInfo(error) });
            return false;
        }
    }

    // ── search-bar answers ──────────────────────────────────────────────

    function answerTurns(question, data, ids = [nextLocalId(), nextLocalId()]) {
        return [
            { ...normalizeMessage({ role: "user", content: question }), id: ids[0] },
            { ...normalizeMessage({ role: "assistant", content: data?.answer }), id: ids[1], citations: answerCitations(data), answer: data },
        ];
    }

    function viewFor(data, request = null) {
        const id = String(data?.history_id || data?.id || "").trim();
        return { historyId: data?.public ? null : id || null, isPublic: Boolean(data?.public), request };
    }

    // Show a stored answer (history, shelf, /answer/<id>, /a/<token>) as
    // this view's first turn.
    function showAnswer(data, { question } = {}) {
        generation += 1;
        const asked = String(question ?? data?.question ?? "").trim();
        const messages = answerTurns(asked, data);
        setState({
            ...initialThreadState(getPrefs()?.community),
            answerView: viewFor(data),
            messages,
            sources: { phase: SOURCES_PHASE.DONE, citations: messages[1].citations, messageId: messages[1].id },
        });
    }

    // A shared conversation (GET /api/public/answer/<token> with
    // `conversation: true`, backend/routes_conversation_share.py): its turns as
    // the owner shared them, read-only. It has no row of the viewer's own and
    // no history id to seed a follow-up from, so it is the public answer view:
    // asking anything starts a thread of their own.
    function showSharedConversation(data) {
        generation += 1;
        const messages = (Array.isArray(data?.messages) ? data.messages : [])
            .map((raw) => ({ ...normalizeMessage({ ...raw, id: null }), id: nextLocalId() }));
        const lastAnswer = [...messages].reverse().find((m) => m.role === "assistant");
        setState({
            ...initialThreadState(getPrefs()?.community),
            answerView: { historyId: null, isPublic: true, request: null, title: String(data?.title || "").trim() },
            messages,
            sources: lastAnswer
                ? { phase: SOURCES_PHASE.DONE, citations: lastAnswer.citations, messageId: lastAnswer.id }
                : { phase: SOURCES_PHASE.IDLE, citations: [], messageId: null },
        });
    }

    // Ask from the search bar through the one-shot /ask (saved to
    // ask_history, works signed out). Resolves the payload, or null when the
    // user moved on meanwhile; rethrows a failure for the caller's side
    // effects (Turnstile reset, logging) after showing it as a FAILED turn.
    async function askSearch(question, request = {}) {
        const text = String(question || "").trim();
        if (!text || typeof askAnswer !== "function") return null;
        const myGeneration = ++generation;
        const ids = [nextLocalId(), nextLocalId()];
        setState({
            ...initialThreadState(getPrefs()?.community),
            answerView: { historyId: null, isPublic: false, request },
            sending: true,
            messages: [
                { ...normalizeMessage({ role: "user", content: text }), id: ids[0], status: MESSAGE_STATUS.PENDING },
                { ...normalizeMessage({ role: "assistant" }), id: ids[1], status: MESSAGE_STATUS.PENDING },
            ],
            sources: { phase: SOURCES_PHASE.SEARCHING, citations: [], messageId: ids[1] },
        });
        try {
            const data = await askAnswer(text, request, { onProgress: progressSink(myGeneration) });
            if (myGeneration !== generation) return null;
            const messages = answerTurns(text, data, ids);
            setState({
                sending: false,
                answerView: viewFor(data, request),
                messages,
                sources: { phase: SOURCES_PHASE.DONE, citations: messages[1].citations, messageId: ids[1] },
            });
            return data;
        } catch (error) {
            if (myGeneration !== generation) return null;
            setState({
                sending: false,
                lastError: { ...errorInfo(error), code: searchErrorCode(error) },
                messages: [{ ...normalizeMessage({ role: "user", content: text }), id: ids[0], status: MESSAGE_STATUS.FAILED }],
                sources: { phase: SOURCES_PHASE.IDLE, citations: [], messageId: null },
            });
            throw error;
        }
    }

    // Re-read the open thread (e.g. "Check again" on an INCOMPLETE answer).
    function refresh() {
        return state.conversation ? open(state.conversation.id) : Promise.resolve(false);
    }

    function setDraftMinhag(minhag) {
        if (state.minhagLocked) return false;
        setState({ draftMinhag: minhag || "All" });
        return true;
    }

    // ── sending ─────────────────────────────────────────────────────────

    // The row is created on the first send. Following up on one of the
    // caller's own search-bar answers seeds it from that ask_history row, so
    // the answer is the thread's first turn server-side too: its local turns
    // are swapped for the stored ones, and keep the answer's payload so its
    // Copy link / feedback stay on screen.
    async function ensureConversation(myGeneration) {
        if (state.conversation) return state.conversation;
        const seedId = seedHistoryId();
        const draft = state.draftMinhag;
        const created = await api.createConversation(seedId ? { fromHistoryId: seedId } : {
            minhag: draft && draft.toLowerCase() !== "all" ? draft : null,
        });
        if (myGeneration !== generation) return null;
        const conversation = normalizeHeader(created);
        const patch = { conversation, minhagLocked: true, answerView: null };
        if (seedId) {
            const seeded = (created?.messages || []).map(normalizeMessage);
            const shown = state.messages.filter((m) => m.answer);
            const payload = shown[0]?.answer || null;
            const lastSeeded = [...seeded].reverse().find((m) => m.role === "assistant");
            if (lastSeeded && payload) lastSeeded.answer = payload;
            const localIds = state.messages.slice(0, 2).map((m) => m.id);
            patch.messages = seeded.length ? replaceLocal(localIds, seeded) : state.messages;
        }
        setState(patch);
        upsertListItem(conversation);
        return conversation;
    }

    function seedHistoryId() {
        const view = state.answerView;
        return view && !view.isPublic && view.historyId ? view.historyId : null;
    }

    function replaceLocal(localIds, replacements) {
        const out = [];
        let inserted = false;
        for (const message of state.messages) {
            if (localIds.includes(message.id)) {
                if (!inserted) {
                    out.push(...replacements);
                    inserted = true;
                }
                continue;
            }
            out.push(message);
        }
        if (!inserted) out.push(...replacements);
        return out;
    }

    function askOptions(mode, language) {
        const prefs = getPrefs() || {};
        return {
            mode: ANSWER_MODE_BY_CHOICE[mode] || mode || prefs.mode || "balanced",
            language: language || prefs.language || "en",
        };
    }

    function knownServerIds() {
        return state.messages.map((m) => m.id).filter(isServerId);
    }

    // Sources state once an answer turn has resolved (COMPLETE or ERROR).
    function sourcesFor(answer) {
        return answer.status === MESSAGE_STATUS.COMPLETE
            ? { phase: SOURCES_PHASE.DONE, citations: answer.citations, messageId: answer.id }
            : { phase: SOURCES_PHASE.UNAVAILABLE, citations: [], messageId: answer.id };
    }

    // Cost-guard refusals (budget_exhausted / ai_paused, with retryAfter) take
    // the same "nothing was saved" path as any other refusal below: the
    // question stays FAILED and lastError carries the code for the UI.
    async function send(question, { mode, language } = {}) {
        const text = String(question || "").trim();
        if (!text || state.sending || state.loadStatus === "loading") return false;

        // A shared answer, or one that was never saved, can't be continued:
        // the question starts a thread of its own instead.
        if (state.answerView && !seedHistoryId()) startNew({ minhag: state.draftMinhag });

        const myGeneration = generation;
        const userLocalId = nextLocalId();
        const answerLocalId = nextLocalId();

        setState({
            sending: true,
            progress: createProgress(),
            lastError: null,
            messages: [
                ...state.messages,
                { ...normalizeMessage({ role: "user", content: text }), id: userLocalId, status: MESSAGE_STATUS.PENDING },
                { ...normalizeMessage({ role: "assistant" }), id: answerLocalId, status: MESSAGE_STATUS.PENDING },
            ],
            sources: { phase: SOURCES_PHASE.SEARCHING, citations: [], messageId: answerLocalId },
        });

        try {
            const conversation = await ensureConversation(myGeneration);
            if (!conversation) return false;
            const knownMessageIds = knownServerIds();

            const result = await api.askInConversation(
                conversation.id,
                { question: text, ...askOptions(mode, language) },
                { knownMessageIds, onProgress: progressSink(myGeneration) },
            );
            if (myGeneration !== generation) return false;

            const userMessage = normalizeMessage(result?.user_message);
            const answer = normalizeMessage({ role: "assistant", ...result?.assistant_message });
            if (!answer.id) answer.id = answerLocalId; // client-only error placeholder
            const header = normalizeHeader(result?.conversation) || state.conversation;
            setState({
                sending: false,
                conversation: header,
                messages: replaceLocal([userLocalId, answerLocalId], [userMessage, answer]),
                sources: sourcesFor(answer),
            });
            upsertListItem(header);
            return true;
        } catch (error) {
            if (myGeneration !== generation) return false;
            if (error?.code === ERROR_CODES.ANSWER_INCOMPLETE && error.userMessage) {
                setState({
                    sending: false,
                    lastError: errorInfo(error),
                    messages: replaceLocal([userLocalId, answerLocalId], [
                        normalizeMessage(error.userMessage),
                        { ...normalizeMessage({ role: "assistant" }), id: answerLocalId, status: MESSAGE_STATUS.INCOMPLETE },
                    ]),
                    sources: { phase: SOURCES_PHASE.UNAVAILABLE, citations: [], messageId: answerLocalId },
                });
                return false;
            }
            // Nothing was saved: keep the question visible as FAILED so the
            // UI can offer Retry (or put it back in the composer).
            setState({
                sending: false,
                lastError: errorInfo(error),
                messages: state.messages
                    .filter((m) => m.id !== answerLocalId)
                    .map((m) => (m.id === userLocalId ? { ...m, status: MESSAGE_STATUS.FAILED } : m)),
                sources: { phase: SOURCES_PHASE.IDLE, citations: [], messageId: null },
            });
            return false;
        }
    }

    // Retry a stored ERROR answer in place (/ask with retry_of): the server
    // reuses the question's user row and supersedes the error row, so the
    // user turn stays where it is and only the answer slot changes. On any
    // failure the ERROR turn comes back, still retryable.
    async function retryStored(index, { mode, language } = {}) {
        const target = state.messages[index];
        const askedIndex = state.messages.slice(0, index).map((m) => m.role).lastIndexOf("user");
        const asked = state.messages[askedIndex];
        if (!asked || !state.conversation) return false;

        const myGeneration = generation;
        const answerLocalId = nextLocalId();
        const knownMessageIds = knownServerIds();
        const pending = { ...normalizeMessage({ role: "assistant" }), id: answerLocalId, status: MESSAGE_STATUS.PENDING };
        setState({
            sending: true,
            progress: createProgress(),
            lastError: null,
            messages: state.messages.map((m) => (m.id === target.id ? pending : m)),
            sources: { phase: SOURCES_PHASE.SEARCHING, citations: [], messageId: answerLocalId },
        });

        try {
            const result = await api.askInConversation(
                state.conversation.id,
                { question: asked.content, ...askOptions(mode, language), retryOf: target.id },
                { knownMessageIds, onProgress: progressSink(myGeneration) },
            );
            if (myGeneration !== generation) return false;

            const userMessage = result?.user_message ? normalizeMessage(result.user_message) : asked;
            const answer = normalizeMessage({ role: "assistant", ...result?.assistant_message });
            if (!answer.id) answer.id = answerLocalId;
            const header = normalizeHeader(result?.conversation) || state.conversation;
            setState({
                sending: false,
                conversation: header,
                messages: state.messages.map((m) => {
                    if (m.id === answerLocalId) return answer;
                    return m.id === asked.id ? userMessage : m;
                }),
                sources: sourcesFor(answer),
            });
            upsertListItem(header);
            return true;
        } catch (error) {
            if (myGeneration !== generation) return false;
            setState({
                sending: false,
                lastError: errorInfo(error),
                messages: state.messages.map((m) => (m.id === answerLocalId ? target : m)),
                sources: { phase: SOURCES_PHASE.UNAVAILABLE, citations: [], messageId: target.id },
            });
            return false;
        }
    }

    // Retry a failed turn. A server-stored ERROR answer (real server id) is
    // retried in place via retryStored. Anything else -- a FAILED local user
    // turn, a local INCOMPLETE answer slot, or an error placeholder the
    // server never stored -- is dropped from view together with its question
    // and re-sent as a fresh send().
    function retry(messageId, options) {
        const index = state.messages.findIndex((m) => m.id === messageId);
        if (index === -1 || state.sending) return Promise.resolve(false);
        const target = state.messages[index];
        if (target.role === "assistant" && target.status === MESSAGE_STATUS.ERROR && isServerId(target.id)) {
            // Only the latest turn: the server stores the new answer with the
            // current timestamp, so retrying an older error would move its
            // answer to the bottom of the thread on the next load.
            if (index !== state.messages.length - 1) return Promise.resolve(false);
            return retryStored(index, options);
        }
        let question = null;
        let dropIds = [];
        if (target.role === "user" && target.status === MESSAGE_STATUS.FAILED && state.answerView && !state.conversation) {
            // A failed search-bar question is asked again the same way.
            return askSearch(target.content, state.answerView.request || {}).then(Boolean, () => false);
        }
        if (target.role === "user" && target.status === MESSAGE_STATUS.FAILED) {
            question = target.content;
            dropIds = [target.id];
        } else if (target.role === "assistant" && [MESSAGE_STATUS.ERROR, MESSAGE_STATUS.INCOMPLETE].includes(target.status)) {
            const asked = state.messages.slice(0, index).reverse().find((m) => m.role === "user");
            if (!asked) return Promise.resolve(false);
            question = asked.content;
            dropIds = [target.id, asked.id];
        } else {
            return Promise.resolve(false);
        }
        setState({ messages: state.messages.filter((m) => !dropIds.includes(m.id)), lastError: null });
        return send(question, options);
    }

    // ── sidebar list + header actions ───────────────────────────────────

    async function loadList({ limit = 20 } = {}) {
        setState({ list: { ...state.list, status: "loading", error: null } });
        try {
            const items = (await api.listConversations({ limit })).map(normalizeHeader).filter(Boolean);
            setState({ list: { status: "ready", items: sortListItems(items), error: null } });
            return true;
        } catch (error) {
            setState({ list: { ...state.list, status: "error", error: errorInfo(error) } });
            return false;
        }
    }

    function applyHeader(header) {
        if (!header) return;
        if (state.conversation?.id === header.id) setState({ conversation: header });
        upsertListItem(header);
    }

    async function rename(id, title) {
        try {
            applyHeader(normalizeHeader(await api.renameConversation(id, title)));
            return true;
        } catch (error) {
            setState({ lastError: errorInfo(error) });
            return false;
        }
    }

    async function setPinned(id, pinned) {
        try {
            applyHeader(normalizeHeader(await api.setConversationPinned(id, pinned)));
            return true;
        } catch (error) {
            setState({ lastError: errorInfo(error) });
            return false;
        }
    }

    // The delete is a server-side soft delete, so it stays undoable:
    // lastDeleted remembers the header and whether it was the open thread
    // (a later remove() replaces it -- only the most recent delete undoes).
    async function remove(id) {
        const wasOpen = state.conversation?.id === id;
        const header = state.list.items.find((item) => item.id === id) || (wasOpen ? state.conversation : null);
        try {
            await api.deleteConversation(id);
        } catch (error) {
            setState({ lastError: errorInfo(error) });
            return false;
        }
        removeListItem(id);
        setState({ lastDeleted: { id, header, wasOpen } });
        if (wasOpen) startNew({ minhag: state.conversation.minhag || state.draftMinhag });
        return true;
    }

    // Undo the most recent remove(): restores the row, puts it back in the
    // sidebar in sort order, and reopens it if it was open -- unless the user
    // has since moved on (opened another thread or started typing into the
    // fresh one). On failure lastDeleted is kept so the undo can be retried.
    async function undoRemove() {
        const pending = state.lastDeleted;
        if (!pending) return false;
        let restored;
        try {
            restored = await api.restoreConversation(pending.id);
        } catch (error) {
            setState({ lastError: errorInfo(error) });
            return false;
        }
        if (state.lastDeleted === pending) setState({ lastDeleted: null });
        const header = normalizeHeader(restored) || pending.header;
        upsertListItem(header);
        if (pending.wasOpen && !state.conversation && state.messages.length === 0) {
            await open(pending.id);
        }
        return true;
    }

    return {
        getState: () => state,
        subscribe(listener) {
            listeners.add(listener);
            return () => listeners.delete(listener);
        },
        startNew,
        open,
        showAnswer,
        showSharedConversation,
        askSearch,
        refresh,
        setDraftMinhag,
        send,
        retry,
        loadList,
        rename,
        setPinned,
        remove,
        undoRemove,
    };
}
