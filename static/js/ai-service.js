import { getState, setState } from "./state.js";

// plan.md §19 Phase 1 (claude_code_prompts.md Prompt 32): this module is the
// canonical POST /ask implementation. It absorbed the retry/timeout resilience
// that used to live only in templates/index.html's inline askWithRetry() --
// ported verbatim (constants, backoff, retryable-status/-error lists, attempt
// telemetry) rather than reimplemented, per §19's reconcile-then-consolidate
// mandate (the inline copy was the *stronger* implementation; the module was
// not).
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
// a raw Response, per Prompt 32 step 3 -- this is the ES-module-native shape
// and the only one `window.ShelahModules` can express cleanly.

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

const AI_RETRYABLE_STATUSES = [502, 503, 504];

function isRetryableUpstreamResponse(response) {
    return !response.ok && AI_RETRYABLE_STATUSES.includes(response.status);
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
// had a fair chance to run (plan.md §23.4). `onRetry(attempt)` is an optional
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

        let headers = await buildAuthHeaders({ "Content-Type": "application/json" });
        let response = await fetchAskWithRetry(requestBody, headers, onRetry);

        // A 401 here almost always means the Clerk token expired mid-session
        // (common on long-lived tabs), not that the AI itself failed -- refresh
        // the token once and retry the full attempt cycle before giving up,
        // matching the pattern saveSemanticBookmark() uses for the same case.
        if (response.status === 401) {
            headers = await buildAuthHeaders({ "Content-Type": "application/json" });
            response = await fetchAskWithRetry(requestBody, headers, onRetry);
        }

        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
            // FastAPI's default HTTPException handler nests structured errors (e.g.
            // turnstile_required) under detail as { error, code } rather than a top
            // -level "error" string -- unwrap that shape before falling back.
            const detail = payload?.detail;
            const message = String(
                payload?.error || (detail && typeof detail === "object" ? detail.error : detail) || "Ask request failed"
            );
            const error = new Error(message);
            error.status = response.status;
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
