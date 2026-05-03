import { getState, setState, subscribe } from "./state.js";
import { askAi } from "./ai-service.js";
import {
    installGlobalErrorBoundary,
    installSemanticBookmarking,
    loadSemanticBookmarks,
    saveSemanticBookmark,
} from "./reader-ui.js";
import { installDailyPrewarm, prewarmDailyStudy } from "./zmanim.js";

function initModules() {
    installGlobalErrorBoundary();
    installSemanticBookmarking();
    installDailyPrewarm();

    void loadSemanticBookmarks();

    window.ShelahModules = {
        askAi,
        loadSemanticBookmarks,
        saveSemanticBookmark,
        prewarmDailyStudy,
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
