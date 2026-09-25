// Network client for the multi-turn conversation routes in
// backend/routes_conversations.py (/api/conversations/*). Owns no DOM and no
// view state -- conversation-store.js is its only intended caller.
//
// Error contract: every call throws a ConversationApiError whose `.code` is
// one of ERROR_CODES (UI branches on that, never on raw statuses), plus
// `.status` when a response arrived and `.retryAfter` (seconds) on a 429 or
// an ai_paused 503. A body `code` the route sets explicitly (cost guards,
// invalid_retry) wins over the status mapping.
//
// Retry policy (ENGINEERING_RULES.md "Timeout & retry"): every request has a
// per-attempt abort timeout and retries transient failures (abort/network/
// 502/503/504) with backoff, but ONLY where a retry can't duplicate a write:
//   - GET/PATCH/DELETE are idempotent: plain bounded retry.
//   - POST /api/conversations (create) is not retried: a lost response would
//     otherwise leave an extra empty thread behind each retry.
//   - POST .../ask saves the user's turn BEFORE the model call, so a blind
//     re-send after a timeout could store the question twice. askInConversation
//     reconciles instead: it re-reads the thread and only re-sends when the
//     question provably never landed (see reconcileAsk below). A retry of a
//     stored error turn (`retryOf`) writes no user row, so it reconciles on
//     the new answer instead (see reconcileRetry). The two cost guards
//     (402 daily_budget_exhausted, 503 ai_paused) reject before any write and
//     are never transient: no reconcile, no re-send.
//   - POST .../restore is idempotent: plain bounded retry.
// A 401 refreshes the Clerk token once and repeats the call -- the auth
// decorator rejects before any write, so that repeat is always safe.

// Client abort ceiling: above the server's AI_TOTAL_BUDGET_SECONDS (45s,
// backend/claude.py) and below Vercel's maxDuration (90s, vercel.json), same
// ordering ai-service.js's /ask client uses.
const REQUEST_TIMEOUT_MS = 60000;
const MAX_ATTEMPTS = 3;
const BACKOFF_MS = 1200;
const RETRYABLE_STATUSES = new Set([502, 503, 504]);

export const ERROR_CODES = Object.freeze({
    UNAUTHORIZED: "unauthorized",       // 401 after a token refresh: sign-in needed
    FORBIDDEN: "forbidden",             // 403: strict-RLS session missing
    NOT_FOUND: "not_found",             // 404: deleted, foreign, or bad id
    INVALID: "invalid",                 // 400: e.g. blank question
    RATE_LIMITED: "rate_limited",       // 429: see .retryAfter
    UNAVAILABLE: "unavailable",         // 503 not retried away: Supabase not configured
    SERVER: "server",                   // other 5xx
    NETWORK: "network",                 // no response after all attempts
    // The question was saved but no answer was stored (yet) -- the client
    // timed out while the server may still be synthesizing. `.userMessage`
    // carries the saved turn; re-reading the thread later may find the answer.
    ANSWER_INCOMPLETE: "answer_incomplete",
    BUDGET_EXHAUSTED: "budget_exhausted", // 402: this user's daily AI spend cap is reached
    AI_PAUSED: "ai_paused",             // 503 code=ai_paused: global cost breaker; see .retryAfter
    INVALID_RETRY: "invalid_retry",     // 400 code=invalid_retry: retryOf isn't a live error turn
});

// Body `code` values the routes set explicitly -> ERROR_CODES.
const CODE_BY_BODY_CODE = Object.freeze({
    daily_budget_exhausted: ERROR_CODES.BUDGET_EXHAUSTED,
    ai_paused: ERROR_CODES.AI_PAUSED,
    invalid_retry: ERROR_CODES.INVALID_RETRY,
});

export class ConversationApiError extends Error {
    constructor(message, { code, status = null, retryAfter = null, userMessage = null } = {}) {
        super(message);
        this.name = "ConversationApiError";
        this.code = code;
        this.status = status;
        this.retryAfter = retryAfter;
        this.userMessage = userMessage;
    }
}

function codeForStatus(status) {
    if (status === 400) return ERROR_CODES.INVALID;
    if (status === 401) return ERROR_CODES.UNAUTHORIZED;
    if (status === 402) return ERROR_CODES.BUDGET_EXHAUSTED;
    if (status === 403) return ERROR_CODES.FORBIDDEN;
    if (status === 404) return ERROR_CODES.NOT_FOUND;
    if (status === 429) return ERROR_CODES.RATE_LIMITED;
    if (status === 503) return ERROR_CODES.UNAVAILABLE;
    return ERROR_CODES.SERVER;
}

async function buildHeaders(withBody) {
    const base = withBody ? { "Content-Type": "application/json" } : {};
    if (typeof window.authHeaders !== "function") return base;
    try {
        return await window.authHeaders(base);
    } catch (_err) {
        return base;
    }
}

function isRetryableNetworkError(error) {
    return error?.name === "AbortError" || error instanceof TypeError;
}

function wait(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

// One fetch with its own abort watchdog. The watchdog is cleared on every
// exit path so no timer outlives the attempt.
async function fetchOnce(path, { method, body }) {
    const headers = await buildHeaders(body !== undefined);
    const abortCtrl = new AbortController();
    const abortTimer = setTimeout(() => abortCtrl.abort(), REQUEST_TIMEOUT_MS);
    try {
        return await fetch(path, {
            method,
            headers,
            signal: abortCtrl.signal,
            body: body === undefined ? undefined : JSON.stringify(body),
        });
    } finally {
        clearTimeout(abortTimer);
    }
}

// Fetch with bounded retry on transient failures (`retry: false` disables
// it), plus the one-time 401 token-refresh repeat. Returns the final
// Response; throws ConversationApiError(NETWORK) when no response arrived.
async function fetchWithPolicy(path, { method = "GET", body, retry = true, onRetry } = {}) {
    const attempts = retry ? MAX_ATTEMPTS : 1;
    let refreshedAuth = false;
    let lastError = null;
    for (let attempt = 1; attempt <= attempts; attempt++) {
        if (attempt > 1) {
            if (typeof onRetry === "function") {
                try { onRetry(attempt); } catch (_err) { /* UI hooks never abort the request */ }
            }
            await wait(BACKOFF_MS * attempt);
        }
        let response;
        try {
            response = await fetchOnce(path, { method, body });
        } catch (error) {
            lastError = error;
            if (isRetryableNetworkError(error)) continue;
            throw error;
        }
        if (response.status === 401 && !refreshedAuth) {
            // window.authHeaders() re-asks Clerk for a token each call; one
            // repeat covers a token that expired on a long-lived tab.
            refreshedAuth = true;
            attempt -= 1;
            continue;
        }
        if (RETRYABLE_STATUSES.has(response.status) && attempt < attempts) {
            lastError = new Error(`Upstream error (${response.status})`);
            continue;
        }
        return response;
    }
    throw new ConversationApiError(lastError?.message || "Network request failed", {
        code: ERROR_CODES.NETWORK,
    });
}

async function readJson(response) {
    try {
        return await response.json();
    } catch (_err) {
        return null;
    }
}

async function toApiError(response) {
    const payload = await readJson(response);
    const retryAfterHeader = response.headers?.get?.("Retry-After");
    const retryAfter = retryAfterHeader ? Number(retryAfterHeader) || null : null;
    return new ConversationApiError(payload?.error || `Request failed (${response.status})`, {
        code: CODE_BY_BODY_CODE[payload?.code] || codeForStatus(response.status),
        status: response.status,
        retryAfter,
    });
}

async function request(path, options) {
    const response = await fetchWithPolicy(path, options);
    if (!response.ok) throw await toApiError(response);
    return readJson(response);
}

function conversationPath(id, suffix = "") {
    return `/api/conversations/${encodeURIComponent(String(id))}${suffix}`;
}

export async function listConversations({ limit = 20 } = {}) {
    const payload = await request(`/api/conversations?limit=${encodeURIComponent(limit)}`);
    return Array.isArray(payload?.items) ? payload.items : [];
}

export function getConversation(id) {
    return request(conversationPath(id));
}

// `minhag` is locked into the thread here and never editable afterwards
// (routes_conversations.create_conversation); "All"/blank means no lens.
export function createConversation({ minhag = null } = {}) {
    return request("/api/conversations", {
        method: "POST",
        body: { minhag: minhag || null },
        retry: false,
    });
}

export function renameConversation(id, title) {
    return request(conversationPath(id), { method: "PATCH", body: { title: String(title ?? "") } });
}

export function setConversationPinned(id, pinned) {
    return request(conversationPath(id), { method: "PATCH", body: { pinned: Boolean(pinned) } });
}

export function deleteConversation(id) {
    return request(conversationPath(id), { method: "DELETE" });
}

// Undo for deleteConversation: clears the soft delete and resolves with the
// conversation header (same shape as PATCH). Idempotent, so it retries.
export function restoreConversation(id) {
    return request(conversationPath(id, "/restore"), { method: "POST" });
}

// After an ask attempt failed without a usable response, decide from the
// thread itself what happened. `knownMessageIds` is every message id the
// caller had before sending, so "new" means "created by this send" without
// trusting client/server clock agreement.
//   -> { outcome: "answered", user_message, assistant_message }
//   -> { outcome: "incomplete", user_message }   (saved, no reply yet)
//   -> { outcome: "not_saved" }                  (safe to re-send)
export async function reconcileAsk(id, question, knownMessageIds) {
    const known = new Set(knownMessageIds || []);
    const thread = await getConversation(id);
    const messages = Array.isArray(thread?.messages) ? thread.messages : [];
    const userIndex = messages.findIndex(
        (m) => m && !known.has(m.id) && m.role === "user" && m.content === question,
    );
    if (userIndex === -1) return { outcome: "not_saved" };
    const reply = messages
        .slice(userIndex + 1)
        .find((m) => m && !known.has(m.id) && m.role === "assistant");
    if (reply) {
        return { outcome: "answered", user_message: messages[userIndex], assistant_message: reply, conversation: thread };
    }
    return { outcome: "incomplete", user_message: messages[userIndex] };
}

function precedingUserIndex(messages, index) {
    for (let i = index - 1; i >= 0; i--) {
        if (messages[i]?.role === "user") return i;
    }
    return -1;
}

// reconcileAsk's counterpart for a retry of stored error turn `retryOf`. A
// retry reuses the question's existing user row, so the only thing a landed
// attempt leaves behind is a new assistant reply after that question (and the
// error row disappears from GET once superseded). "New" again means not in
// `knownMessageIds`; a reply to a question that is itself new belongs to some
// other send, not this retry.
//   -> { outcome: "answered", user_message, assistant_message, conversation }
//   -> { outcome: "not_saved" }                  (safe to re-send retry_of)
export async function reconcileRetry(id, retryOf, knownMessageIds) {
    const known = new Set(knownMessageIds || []);
    const thread = await getConversation(id);
    const messages = Array.isArray(thread?.messages) ? thread.messages : [];
    const errorIndex = messages.findIndex((m) => m && m.id === retryOf);
    const askedIndex = errorIndex === -1 ? -1 : precedingUserIndex(messages, errorIndex);
    for (let i = 0; i < messages.length; i++) {
        const reply = messages[i];
        if (!reply || reply.role !== "assistant" || known.has(reply.id)) continue;
        const userIndex = precedingUserIndex(messages, i);
        if (userIndex === -1 || !known.has(messages[userIndex].id)) continue;
        // While the error row is still visible, only its own question counts.
        if (errorIndex !== -1 && userIndex !== askedIndex) continue;
        return { outcome: "answered", user_message: messages[userIndex], assistant_message: reply, conversation: thread };
    }
    return { outcome: "not_saved" };
}

function isTransientAskFailure(failure) {
    if (failure.code === ERROR_CODES.NETWORK) return true;
    // ai_paused is a 503 too, but a deliberate refusal: re-sending can't help.
    return RETRYABLE_STATUSES.has(failure.status) && failure.code !== ERROR_CODES.AI_PAUSED;
}

function answeredResult(state) {
    return {
        user_message: state.user_message,
        assistant_message: state.assistant_message,
        conversation: state.conversation,
    };
}

// POST .../ask with reconcile-before-resend (see module header). Resolves
// with the route's 201 body: { user_message, assistant_message, conversation }
// (+ superseded_message_id for a retry). assistant_message.status === "error"
// is a normal resolution, not a throw -- the route stores a visible,
// retryable failure turn in that case.
//
// `retryOf` (a stored assistant message id with status "error") asks the
// route to answer that turn's question again in place: no new user row, the
// error row is superseded. If a re-send after a transient failure gets
// invalid_retry, the earlier attempt must have landed and superseded the row
// -- so the thread is re-read and that answer returned. The same re-read runs
// when the FIRST attempt gets invalid_retry (another tab retried the turn
// already); it only throws when no fresh answer is found.
export async function askInConversation(id, { question, mode, language, retryOf } = {}, { knownMessageIds = [], onRetry } = {}) {
    const body = { question: String(question || "").trim(), mode, language };
    if (retryOf) body.retry_of = String(retryOf);
    const path = conversationPath(id, "/ask");
    for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
        if (attempt > 1) {
            if (typeof onRetry === "function") {
                try { onRetry(attempt); } catch (_err) { /* UI hooks never abort the request */ }
            }
            await wait(BACKOFF_MS * attempt);
        }
        let response = null;
        let error = null;
        try {
            response = await fetchWithPolicy(path, { method: "POST", body, retry: false });
        } catch (err) {
            if (!(err instanceof ConversationApiError)) throw err;
            error = err;
        }
        if (response?.ok) return readJson(response);
        // The body is read once, here: its `code` decides transient vs not.
        const failure = error || await toApiError(response);
        if (retryOf && failure.code === ERROR_CODES.INVALID_RETRY) {
            let state;
            try {
                state = await reconcileRetry(id, body.retry_of, knownMessageIds);
            } catch (_reconcileError) {
                throw failure;
            }
            if (state.outcome === "answered") return answeredResult(state);
            throw failure;
        }
        if (!isTransientAskFailure(failure)) throw failure;

        // Transient failure: the server may or may not have saved the turn.
        let state;
        try {
            state = retryOf
                ? await reconcileRetry(id, body.retry_of, knownMessageIds)
                : await reconcileAsk(id, body.question, knownMessageIds);
        } catch (_reconcileError) {
            // Can't tell whether the question landed -- re-sending could
            // duplicate it, so surface the failure instead of guessing.
            throw failure;
        }
        if (state.outcome === "answered") return answeredResult(state);
        if (state.outcome === "incomplete") {
            throw new ConversationApiError("The question was saved but the answer did not finish.", {
                code: ERROR_CODES.ANSWER_INCOMPLETE,
                userMessage: state.user_message,
            });
        }
        // not_saved: nothing landed, so the next attempt is a clean re-send.
    }
    throw new ConversationApiError("Network request failed", { code: ERROR_CODES.NETWORK });
}
