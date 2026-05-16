const listeners = new Set();

function isPlainObject(value) {
    return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function deepMerge(target, patch) {
    if (!isPlainObject(patch)) {
        return target;
    }

    for (const [key, value] of Object.entries(patch)) {
        if (isPlainObject(value)) {
            if (!isPlainObject(target[key])) {
                target[key] = {};
            }
            deepMerge(target[key], value);
        } else {
            target[key] = value;
        }
    }
    return target;
}

const state = isPlainObject(window.appState) ? window.appState : {
    prefs: {
        mode: "balanced",
        community: "All",
    },
    ai: {
        pending: false,
        lastResponse: null,
        lastError: null,
    },
    dailyStudy: {
        refs: [],
        prewarmedAt: null,
    },
    semanticBookmarks: {
        items: [],
        lastUpdatedAt: null,
    },
};

window.appState = state;

function emitChange() {
    const snapshot = getState();
    for (const listener of listeners) {
        try {
            listener(snapshot);
        } catch (_err) {
            // Keep state updates resilient to consumer exceptions.
        }
    }
}

export function getState() {
    return state;
}

export function setState(patch) {
    deepMerge(state, patch || {});
    window.appState = state;
    emitChange();
    return state;
}

export function subscribe(listener) {
    if (typeof listener !== "function") {
        return () => { };
    }
    listeners.add(listener);
    return () => listeners.delete(listener);
}

window.ShelahState = {
    getState,
    setState,
    subscribe,
    // NOTE: raw `state` is intentionally omitted — use getState() to read state.
};

export { state };
