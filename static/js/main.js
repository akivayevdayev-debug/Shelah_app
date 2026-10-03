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
    getSunset,
    refreshZmanimDisplay,
} from "./zmanim.js";
import { installRouter, readRoute } from "./router.js";
import { installConversationUI } from "./conversation-ui.js";
import * as sourceCards from "./source-cards.js";
import * as commentaryPreload from "./commentary-preload.js";
import { installAnswerLink, answerIdOf } from "./answer-link.js";
import { createAnswerShare, installShareState } from "./answer-share.js";
import * as askHistory from "./ask-history.js";
import { createSiddur } from "./siddur.js";

// Source-card markup for the conversation panel (and any classic-script caller).
window.ShelahSourceCards = sourceCards;
// The reader's commentary sidebar: finds the verse in view and preloads its
// commentary (index.html's ensureCommentaryPreloader builds the preloader).
window.ShelahCommentaryPreload = commentaryPreload;
// The /history page and the shelf's stored-answer entries (index.html's
// displayAskHistoryPage / openShelfItem / promoteShelfAsk).
window.ShelahAskHistory = askHistory;

// "Copy link" + public share state for one placement, backed by one share
// store. A public (/a/<token>) answer has no history id, so it stays hidden.
function installAnswerLinkPlacement(root, share) {
    const link = installAnswerLink(root, { getLinkUrl: share.linkFor });
    const state = installShareState(root, share);
    return {
        show(data) {
            link.show(data);
            state.show(answerIdOf(data));
        },
        hide() {
            link.hide();
            state.hide();
        },
    };
}

// installZmanim()'s dependency contract: the module takes these eight inline
// classic-script globals as an explicit `deps` object instead of reaching
// for `window.*` itself.
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
        setHebrewDate: window.setLocationHebrewDate,
    };
}

// The siddur's dependencies on the classic script (static/js/siddur.js),
// gathered here like buildZmanimDeps.
function buildSiddurDeps() {
    return {
        t: (en, he) => window.t(en, he),
        isHebrewMode: () => window.isHebrewMode(),
        escapeHtml: (text) => window.escapeHtml(text),
        applyHebrewDisplaySettings: (text) => window.applyHebrewDisplaySettings?.(text) ?? text,
        getPrefs: () => window.appState?.prefs || {},
        getSunset,
        getLocation: getZmanimLocation,
        reduceMotion: () => Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)").matches),
        // Scrolling a service moves the URL to the section being read:
        // replaceState, so Back leaves the service rather than stepping
        // through its sections (audit U4).
        // Only while the URL is a siddur page: a late scroll event must never
        // pull another view's URL back to the siddur.
        replaceRoute: (value) => {
            const router = window.ShelahRouter;
            if (router?.readRoute().siddur) router.pushRoute({ siddur: value }, { replace: true });
        },
    };
}

function initModules() {
    installGlobalErrorBoundary();
    installSemanticBookmarking();
    installDailyPrewarm();
    void installZmanim(buildZmanimDeps());

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
    // Before the initial hydrate, so a /chat/<id> link finds the UI ready.
    installConversationUI();
    // "Copy link" on a stored answer: conversation-ui.js moves the control
    // into the answer turn showing it and calls .show per answer. The copied
    // link is public (/a/<token>, static/js/answer-share.js).
    const answerShare = createAnswerShare();
    // The siddur view (index.html displaySiddur) and its menus.
    window.ShelahSiddur = createSiddur({ deps: buildSiddurDeps() });
    window.renderSiddurMenus?.();

    window.ShelahAnswerLink = {
        panel: installAnswerLinkPlacement(document.getElementById("convAnswerLink"), answerShare),
    };

    // `saved`: where the reader was on the entry Back/Forward returns to
    // (index.html flushViewScroll, audit U-7).
    installRouter((route, { direction, saved, left } = {}) => window.hydrateRoute?.(route, { direction, saved, left }));
    window.hydrateRoute?.(readRoute(), { isInitial: true });
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initModules, { once: true });
} else {
    initModules();
}
