import { getState, setState } from "./state.js";

const CLIENT_ERROR_ENDPOINT = "/api/client-errors";
const SEMANTIC_BOOKMARKS_ENDPOINT = "/api/bookmarks/semantic";

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

function hasAuthorizationHeader(headers) {
    if (!headers || typeof headers !== "object") {
        return false;
    }

    return Boolean(headers.Authorization || headers.authorization);
}

async function postClientError(payload) {
    try {
        await fetch(CLIENT_ERROR_ENDPOINT, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify(payload),
        });
    } catch (_err) {
        // Best effort telemetry should never break the UI.
    }
}

export function installGlobalErrorBoundary() {
    window.addEventListener("error", (event) => {
        void postClientError({
            type: "window.error",
            message: String(event?.message || "Unknown client error").slice(0, 500),
            stack: String(event?.error?.stack || "").slice(0, 8000),
            component: "global",
            url: String(event?.filename || window.location?.href || "").slice(0, 400),
            line: Number(event?.lineno || 0),
            column: Number(event?.colno || 0),
        });
    });

    window.addEventListener("unhandledrejection", (event) => {
        const reason = event?.reason;
        void postClientError({
            type: "unhandledrejection",
            message: String(reason?.message || reason || "Unhandled promise rejection").slice(0, 500),
            stack: String(reason?.stack || "").slice(0, 8000),
            component: "promise",
            url: String(window.location?.href || "").slice(0, 400),
        });
    });
}

function readCurrentSemanticContext() {
    const state = getState();
    const currentView = state?.currentView || null;
    const ref = String(currentView?.value || "").trim();
    const label = String(currentView?.label || ref || "").trim();

    const selectedText = String(window.getSelection?.()?.toString?.() || "").trim();
    let segmentText = selectedText;

    if (!segmentText) {
        const readerSources = document.getElementById("readerSources");
        const rawText = String(readerSources?.innerText || "").trim();
        segmentText = rawText.slice(0, 1200);
    }

    return {
        ref,
        label,
        segmentText,
    };
}

function pulseButton(button, text, className) {
    if (!button) return;
    const originalText = button.dataset.originalText || button.textContent || "";
    button.dataset.originalText = originalText;
    button.textContent = text;
    button.classList.add(className);

    window.setTimeout(() => {
        button.textContent = originalText;
        button.classList.remove(className);
    }, 1200);
}

export async function loadSemanticBookmarks() {
    try {
        const headers = await buildAuthHeaders({
            "Content-Type": "application/json",
        });
        if (!hasAuthorizationHeader(headers)) {
            return [];
        }

        const response = await fetch(SEMANTIC_BOOKMARKS_ENDPOINT, {
            method: "GET",
            headers,
        });
        if (!response.ok) {
            return [];
        }

        const payload = await response.json().catch(() => ({}));
        const items = Array.isArray(payload?.items) ? payload.items : [];
        setState({
            semanticBookmarks: {
                items,
                lastUpdatedAt: new Date().toISOString(),
            },
        });
        return items;
    } catch (_err) {
        return [];
    }
}

export async function saveSemanticBookmark(noteText = "") {
    const { ref, label, segmentText } = readCurrentSemanticContext();
    if (!ref && !segmentText) {
        throw new Error("Open a text or select content before saving a study note.");
    }

    const payload = {
        ref,
        label,
        segment_text: segmentText,
        notes: String(noteText || "").trim(),
    };

    const headers = await buildAuthHeaders({
        "Content-Type": "application/json",
    });
    if (!hasAuthorizationHeader(headers)) {
        throw new Error("Authentication required. Please sign in first.");
    }

    const response = await fetch(SEMANTIC_BOOKMARKS_ENDPOINT, {
        method: "POST",
        headers,
        body: JSON.stringify(payload),
    });

    const data = await response.json().catch(() => ({}));
    if (response.status === 401) {
        throw new Error("Authentication required. Please sign in first.");
    }

    if (!response.ok || !data?.ok) {
        throw new Error(String(data?.error || "Failed to save semantic bookmark"));
    }

    await loadSemanticBookmarks();
    return data;
}

export function installSemanticBookmarking() {
    const button = document.getElementById("semanticBookmarkBtn");
    if (!button) return;

    button.addEventListener("click", async () => {
        const note = window.prompt("Optional note for this bookmark:", "") || "";
        try {
            await saveSemanticBookmark(note);
            pulseButton(button, "Saved", "bg-emerald-700");
            button.classList.add("text-white");
            window.setTimeout(() => button.classList.remove("text-white"), 1200);
        } catch (error) {
            pulseButton(button, "Retry", "bg-rose-700");
            button.classList.add("text-white");
            window.setTimeout(() => button.classList.remove("text-white"), 1200);
            await postClientError({
                type: "semantic-bookmark",
                message: String(error?.message || error || "Semantic bookmark failed").slice(0, 500),
                component: "reader-ui",
                url: String(window.location?.href || "").slice(0, 400),
            });
        }
    });
}
