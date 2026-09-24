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
    getZmanimLocation,
    refreshZmanimDisplay,
} from "./zmanim.js";
import { installRouter, readRoute } from "./router.js";

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

    window.ShelahModules = {
        askAi,
        loadSemanticBookmarks,
        saveSemanticBookmark,
        prewarmDailyStudy,
        fetchZmanimAPI,
        setZmanimLocationLabel,
        setCurrentZmanimLocationLabel,
        cacheZmanimLocation,
        getZmanimLocation,
        refreshZmanimDisplay,
        getState,
        setState,
        subscribe,
    };

    // installRouter's handler fires on popstate (back/forward); the initial
    // hydrate call handles the URL the page was actually loaded with, since
    // popstate never fires for that first load.
    installRouter((route) => window.hydrateRoute?.(route));
    window.hydrateRoute?.(readRoute(), { isInitial: true });
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initModules, { once: true });
} else {
    initModules();
}
