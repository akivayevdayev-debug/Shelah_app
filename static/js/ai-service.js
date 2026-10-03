import { getState, setState } from "./state.js";
import { ACCEPT_PROGRESS, isProgressResponse, readProgressStream } from "./ask-progress.js";

// This module is the canonical POST /ask implementation. It absorbed the
// retry/timeout resilience that used to live only in templates/index.html's
// inline askWithRetry() -- ported verbatim (constants, backoff,
// retryable-status/-error lists, attempt telemetry) rather than
// reimplemented, because the inline copy was the *stronger* implementation;
// the module was not.
//
// Return contract: askAi() returns the parsed JSON payload on success. On
// failure it throws an Error whose:
//   - `.message` is the human-readable failure reason
//   - `.status` is the HTTP status code, when a response was received at all
//     (absent for network/timeout failures where no response ever arrived)
//   - `.code` is the backend's structured error code (e.g. "turnstile_required"),
//     when the response body carried one under `detail.code`
//   - `.attempts` is the number of fetch attempts made, when the failure came
//     from exhausting retries on a network error (AbortError/TypeError) rather
//     than from a completed HTTP response
// Callers branch on `.code` / `.status` for UI handling instead of inspecting
// a raw Response -- this is the ES-module-native shape
// and the only one `window.ShelahModules` can express cleanly.
//
// Live progress: askAi() asks the server for an NDJSON stream (see
// ask-progress.js) and reports each pipeline step to `options.onProgress`
// (one event per call). The stream ends in the same payload or the same
// failure shape as the plain response, and a server that answers with plain
// JSON is handled exactly as before.

const AI_REQUEST_TIMEOUT_MS = 60000;
const AI_MAX_ATTEMPTS = 3;

async function buildAuthHeaders(baseHeaders) {
    if (typeof window.authHeaders === "function") {
        try {
            return await window.authHeaders(baseHeaders);
        } catch (_err) {
            return baseHeaders;
        }
    }
    return baseHeaders;
}

const AI_RETRYABLE_STATUSES = new Set([502, 503, 504]);

function isRetryableUpstreamResponse(response) {
    return !response.ok && AI_RETRYABLE_STATUSES.has(response.status);
}

function isRetryableNetworkError(error) {
    return error?.name === "AbortError" || error instanceof TypeError;
}

// Reports the upcoming retry to the caller's UI hook, then backs off.
async function waitBeforeRetry(onRetry, attempt) {
    if (typeof onRetry === "function") {
        try {
            onRetry(attempt);
        } catch (_err) {
            // Retry-UI hooks must never abort the request they're reporting on.
        }
    }
    await new Promise((resolve) => setTimeout(resolve, 1200 * attempt));
}

// POSTs /ask with a bounded per-attempt timeout and automatic retry on
// transient failures (abort/network/502/503/504) -- never on 4xx or a clean
// response. Prevents the "times out after a couple of tries" failure mode
// where the client gave up before the server's own graceful-fallback budget
// had a fair chance to run. `onRetry(attempt)` is an optional
// caller-supplied hook for retry-in-progress UI; this module owns no DOM.
async function fetchAskWithRetry(requestBody, headers, onRetry) {
    let lastError = null;
    for (let attempt = 1; attempt <= AI_MAX_ATTEMPTS; attempt++) {
        if (attempt > 1) {
            await waitBeforeRetry(onRetry, attempt);
        }

        const abortCtrl = new AbortController();
        const abortTimer = setTimeout(() => abortCtrl.abort(), AI_REQUEST_TIMEOUT_MS);
        try {
            const response = await fetch("/ask", {
                method: "POST",
                headers,
                signal: abortCtrl.signal,
                body: requestBody,
            });
            if (isRetryableUpstreamResponse(response) && attempt < AI_MAX_ATTEMPTS) {
                lastError = new Error(`Upstream error (${response.status})`);
                continue;
            }
            return response;
        } catch (error) {
            lastError = error;
            if (isRetryableNetworkError(error) && attempt < AI_MAX_ATTEMPTS) {
                continue;
            }
            error.attempts = attempt;
            throw error;
        } finally {
            clearTimeout(abortTimer);
        }
    }
    if (lastError) lastError.attempts = AI_MAX_ATTEMPTS;
    throw lastError || new Error("Request failed after retries");
}

export async function askAi(question, options = {}) {
    const q = String(question || "").trim();
    if (!q) {
        throw new Error("Question is required");
    }

    const state = getState();
    const mode = String(options.mode || state?.prefs?.mode || "balanced");
    const community = String(options.community || state?.prefs?.community || "All");
    const language = String(options.language || state?.prefs?.language || "en");
    const onRetry = typeof options.onRetry === "function" ? options.onRetry : null;
    const onProgress = typeof options.onProgress === "function" ? options.onProgress : null;

    setState({
        ai: {
            pending: true,
            lastError: null,
            lastAskedAt: new Date().toISOString(),
        },
    });

    try {
        const requestBody = JSON.stringify({
            question: q,
            mode,
            community,
            language,
            // Set by the Turnstile widget's callback in templates/index.html
            // (backend/turnstile.py); optional, ignored server-side unless an
            // anonymous caller has crossed the hourly threshold.
            turnstile_token: window.__turnstileToken || "",
        });

        const baseHeaders = { "Content-Type": "application/json", Accept: ACCEPT_PROGRESS };
        let headers = await buildAuthHeaders(baseHeaders);
        let response = await fetchAskWithRetry(requestBody, headers, onRetry);

        // A 401 here almost always means the Clerk token expired mid-session
        // (common on long-lived tabs), not that the AI itself failed -- refresh
        // the token once and retry the full attempt cycle before giving up,
        // matching the pattern saveSemanticBookmark() uses for the same case.
        if (response.status === 401) {
            headers = await buildAuthHeaders(baseHeaders);
            response = await fetchAskWithRetry(requestBody, headers, onRetry);
        }

        let payload;
        let failedStatus = response.ok ? 0 : response.status;
        if (response.ok && isProgressResponse(response)) {
            const terminal = await readProgressStream(response, onProgress, { timeoutMs: AI_REQUEST_TIMEOUT_MS });
            if (terminal.type === "result") {
                payload = terminal.payload;
            } else {
                // A failure after the stream started: same body shape the
                // plain response would have had, just carried in the last line.
                failedStatus = Number(terminal.status) || 500;
                payload = { detail: terminal.detail };
            }
        } else {
            payload = await response.json().catch(() => ({}));
        }
        if (failedStatus) {
            // FastAPI's default HTTPException handler nests structured errors (e.g.
            // turnstile_required) under detail as { error, code } rather than a top
            // -level "error" string -- unwrap that shape before falling back.
            const detail = payload?.detail;
            const message = String(
                payload?.error || (detail && typeof detail === "object" ? detail.error : detail) || "Ask request failed"
            );
            const error = new Error(message);
            error.status = failedStatus;
            if (detail && typeof detail === "object" && detail.code) {
                error.code = detail.code;
            }
            throw error;
        }

        setState({
            ai: {
                pending: false,
                lastError: null,
                lastResponse: payload,
                lastAnsweredAt: new Date().toISOString(),
            },
        });

        return payload;
    } catch (error) {
        setState({
            ai: {
                pending: false,
                lastError: String(error?.message || error || "Unknown ask failure"),
            },
        });
        throw error;
    }
}
