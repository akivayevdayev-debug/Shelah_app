import { setState } from "./state.js";

function normalizeDailyRef(value) {
    const ref = String(value || "").trim();
    return ref ? ref : null;
}

function collectRefsFromPayload(payload) {
    if (!payload || typeof payload !== "object") {
        return [];
    }

    const refs = new Set();

    const addRef = (candidate) => {
        const normalized = normalizeDailyRef(candidate);
        if (normalized) {
            refs.add(normalized);
        }
    };

    addRef(payload?.daf_yomi?.ref);
    addRef(payload?.rambam?.ref);
    addRef(payload?.parasha?.ref);
    addRef(payload?.parasha_ref);

    const values = Object.values(payload);
    for (const value of values) {
        if (typeof value === "string") {
            addRef(value);
            continue;
        }
        if (Array.isArray(value)) {
            for (const item of value) {
                if (typeof item === "string") {
                    addRef(item);
                } else if (item && typeof item === "object") {
                    addRef(item.ref);
                    addRef(item.title);
                }
            }
            continue;
        }
        if (value && typeof value === "object") {
            addRef(value.ref);
        }
    }

    return Array.from(refs).slice(0, 9);
}

async function fetchDailyStudyRefs() {
    const response = await fetch("/api/daily-study", {
        method: "GET",
        credentials: "same-origin",
    });
    if (!response.ok) {
        return [];
    }

    const payload = await response.json().catch(() => ({}));
    return collectRefsFromPayload(payload);
}

function postPrewarmToServiceWorker(refs) {
    if (!("serviceWorker" in navigator) || !Array.isArray(refs) || !refs.length) {
        return;
    }

    const message = {
        type: "PREWARM_DAILY",
        refs,
    };

    if (navigator.serviceWorker.controller) {
        navigator.serviceWorker.controller.postMessage(message);
        return;
    }

    navigator.serviceWorker.ready
        .then((registration) => {
            if (registration?.active) {
                registration.active.postMessage(message);
            }
        })
        .catch(() => {
            // Ignore service worker readiness failures.
        });
}

async function prefetchRefText(ref) {
    const encoded = encodeURIComponent(ref);
    await fetch(`/api/text/${encoded}?autotranslate=0`, {
        method: "GET",
        credentials: "same-origin",
    }).catch(() => null);
}

export async function prewarmDailyStudy() {
    const refs = await fetchDailyStudyRefs();
    if (!refs.length) {
        return [];
    }

    postPrewarmToServiceWorker(refs);
    await Promise.allSettled(refs.map((ref) => prefetchRefText(ref)));

    setState({
        dailyStudy: {
            refs,
            prewarmedAt: new Date().toISOString(),
        },
    });

    return refs;
}

export function installDailyPrewarm() {
    const run = () => {
        window.setTimeout(() => {
            void prewarmDailyStudy();
        }, 2200);
    };

    if (document.readyState === "complete") {
        run();
    } else {
        window.addEventListener("load", run, { once: true });
    }
}
