// Live status for an AI answer while it is being prepared. Owns no DOM.
//
// The server (backend/ask_progress.py) reports each pipeline step as it starts
// and ends -- one NDJSON line per event, then a terminal `result` or `error`
// line -- when the client asks for `application/x-ndjson`. This module turns
// those lines into a small state object and a view (which step is running,
// which are finished), and reads the stream for the two ask clients
// (ai-service.js and conversation-api.js).
//
// Wire events:
//   { type: "stage", stage, state: "start" | "done" }
//   { type: "ping" }                                   (keep-alive, ignored)
//   { type: "result", payload | body, status? }        (terminal)
//   { type: "error",  status, detail | body }          (terminal)

export const NDJSON_TYPE = "application/x-ndjson";

// Asking for the stream never costs a fallback: a server (or proxy) that
// ignores it answers with plain JSON, which both clients still handle.
export const ACCEPT_PROGRESS = `${NDJSON_TYPE}, application/json;q=0.5`;

// [English, Hebrew] per step. `active` reads as what is happening now; `done`
// is the short noun shown once that step has finished. "thinking" has no
// `done`: it ends with the answer itself.
const STAGES = Object.freeze({
    sources: { active: ["Searching the sources", "מחפשים במקורות"], done: ["Sources", "מקורות"] },
    commentary: {
        active: ["Checking Halachipedia and other references", "בודקים בהלכיפדיה ובמקורות נוספים"],
        done: ["References", "מקורות נוספים"],
    },
    customs: { active: ["Checking community customs", "בודקים מנהגי קהילות"], done: ["Customs", "מנהגים"] },
    times: {
        active: ["Looking up today's zmanim and dates", "בודקים זמנים ותאריכים להיום"],
        done: ["Zmanim and dates", "זמנים ותאריכים"],
    },
    thinking: { active: ["Thinking it through", "חושבים על התשובה"], done: null },
});

// Before the first event arrives (the request is still being accepted).
export const WAITING_LABEL = Object.freeze(["Getting started", "מתחילים"]);

// Which running step names the status line when several run at once: the
// ones that usually take longest first, so the line changes only when that
// work genuinely finishes.
const ACTIVE_PRIORITY = Object.freeze(["thinking", "sources", "commentary", "customs", "times"]);

export const STAGE_IDS = Object.freeze(Object.keys(STAGES));

export function createProgress() {
    return { order: [], status: {} };
}

// Pure: returns a new state, or the same object when the event changes nothing.
export function applyProgressEvent(state, event) {
    if (!event || event.type !== "stage" || !Object.hasOwn(STAGES, event.stage)) return state;
    const next = event.state === "start" ? "active" : event.state === "done" ? "done" : null;
    if (!next || state.status[event.stage] === next) return state;
    return {
        order: state.order.includes(event.stage) ? state.order : [...state.order, event.stage],
        status: { ...state.status, [event.stage]: next },
    };
}

// What the status line should show: `active` is the step to name (null before
// anything has started), `done` the finished steps in the order they began.
export function progressView(state) {
    const running = ACTIVE_PRIORITY.find((id) => state.status[id] === "active") || null;
    const done = state.order.filter((id) => state.status[id] === "done" && STAGES[id].done);
    if (running) return { active: running, done };
    // Everything reported has finished but no answer yet (between the last
    // lookup and the model call): the model is the only thing left to wait on.
    return { active: state.order.length ? "thinking" : null, done };
}

function pick(pair, lang) {
    return String(lang || "").toLowerCase().startsWith("he") ? pair[1] : pair[0];
}

export function activeLabel(stageId, lang) {
    return pick(stageId && STAGES[stageId] ? STAGES[stageId].active : WAITING_LABEL, lang);
}

export function doneLabel(stageId, lang) {
    const pair = STAGES[stageId]?.done;
    return pair ? pick(pair, lang) : "";
}

// Reads an NDJSON ask response to its terminal event, reporting every
// non-terminal event to `onEvent`. Resolves with the terminal event
// ({type: "result"} or {type: "error"}); rejects when the stream ends
// without one (connection cut, or the deadline passed) so the caller can treat
// it like any other interrupted request.
export async function readProgressStream(response, onEvent, { timeoutMs = 60000 } = {}) {
    const reader = response.body?.getReader?.();
    let terminal = null;
    let timedOut = false;

    const handleLine = (line) => {
        const text = line.trim();
        if (!text || terminal) return;
        let event;
        try { event = JSON.parse(text); } catch (_err) { return; }
        if (!event || typeof event !== "object") return;
        if (event.type === "result" || event.type === "error") {
            terminal = event;
            return;
        }
        if (typeof onEvent === "function") {
            try { onEvent(event); } catch (_err) { /* progress UI never aborts the answer */ }
        }
    };

    if (reader) {
        const decoder = new TextDecoder();
        const timer = setTimeout(() => {
            timedOut = true;
            reader.cancel().catch(() => {});
        }, timeoutMs);
        let buffer = "";
        try {
            for (;;) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split("\n");
                buffer = lines.pop();
                lines.forEach(handleLine);
                if (terminal) break;
            }
            buffer += decoder.decode();
            handleLine(buffer);
        } finally {
            clearTimeout(timer);
            if (terminal) reader.cancel().catch(() => {});
        }
    } else {
        // No streaming body (older engines, test doubles): the whole response
        // is still NDJSON, so read it in one go.
        String(await response.text()).split("\n").forEach(handleLine);
    }

    if (terminal) return terminal;
    const error = new Error(timedOut ? "The answer took too long." : "The answer was interrupted.");
    error.interrupted = true;
    throw error;
}

export function isProgressResponse(response) {
    return String(response?.headers?.get?.("content-type") || "").toLowerCase().includes(NDJSON_TYPE);
}
