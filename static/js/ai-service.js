import { getState, setState } from "./state.js";

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

export async function askAi(question, options = {}) {
    const q = String(question || "").trim();
    if (!q) {
        throw new Error("Question is required");
    }

    const state = getState();
    const mode = String(options.mode || state?.prefs?.mode || "balanced");
    const community = String(options.community || state?.prefs?.community || "All");
    const language = String(options.language || state?.prefs?.language || "en");

    setState({
        ai: {
            pending: true,
            lastError: null,
            lastAskedAt: new Date().toISOString(),
        },
    });

    try {
        const headers = await buildAuthHeaders({
            "Content-Type": "application/json",
        });

        const response = await fetch("/ask", {
            method: "POST",
            headers,
            body: JSON.stringify({
                question: q,
                mode,
                community,
                language,
            }),
        });

        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
            const message = String(payload?.error || payload?.detail || "Ask request failed");
            throw new Error(message);
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
