import { getState, setState, subscribe } from "./state.js";
import { askAi } from "./ai-service.js";
import {
    installGlobalErrorBoundary,
    installSemanticBookmarking,
    loadSemanticBookmarks,
    saveSemanticBookmark,
} from "./reader-ui.js";
import {
    installDailyPrewarm,
    prewarmDailyStudy,
    installZmanim,
    fetchZmanimAPI,
    setZmanimLocationLabel,
    setCurrentZmanimLocationLabel,
    cacheZmanimLocation,
    refreshZmanimDisplay,
} from "./zmanim.js";

// installZmanim()'s dependency contract (plan.md §19 Phase 2, §19.9
// constraint 3): the module takes these seven inline classic-script globals
// as an explicit `deps` object instead of reaching for `window.*` itself.
// This is the one place that reach happens -- main.js is the documented
// wiring boundary between the classic script and the ES modules, same as
// buildAuthHeaders's window.authHeaders reach elsewhere, but centralized
// here instead of repeated inside the module's own function bodies.
function buildZmanimDeps() {
    return {
        t: window.t,
        isHebrewMode: window.isHebrewMode,
        translateHolidayName: window.translateHolidayName,
        formatOmerLabel: window.formatOmerLabel,
        formatWeeklyShabbatLabel: window.formatWeeklyShabbatLabel,
        translateShabbatWarning: window.translateShabbatWarning,
        escapeHtml: window.escapeHtml,
    };
}

function initModules() {
    installGlobalErrorBoundary();
    installSemanticBookmarking();
    installDailyPrewarm();
    installZmanim(buildZmanimDeps());

    void loadSemanticBookmarks();

    window.ShelahModules = {
        askAi,
        loadSemanticBookmarks,
        saveSemanticBookmark,
        prewarmDailyStudy,
        fetchZmanimAPI,
        setZmanimLocationLabel,
        setCurrentZmanimLocationLabel,
        cacheZmanimLocation,
        refreshZmanimDisplay,
        getState,
        setState,
        subscribe,
    };
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initModules, { once: true });
} else {
    initModules();
}
