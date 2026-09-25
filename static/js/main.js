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
import { installConversationUI } from "./conversation-ui.js";
import * as sourceCards from "./source-cards.js";
import { installAnswerLink, answerIdOf } from "./answer-link.js";
import { createAnswerShare, installShareState } from "./answer-share.js";
import * as askHistory from "./ask-history.js";

// The classic script's AI modal (populateAiModal) builds its source cards
// from the same module as the conversation panel.
window.ShelahSourceCards = sourceCards;
// The /history page and the shelf's stored-answer entries (index.html's
// displayAskHistoryPage / openShelfItem / promoteShelfAsk).
window.ShelahAskHistory = askHistory;

// "Copy link" + public share state for one placement. Both placements share
// one share store, so sharing or revoking in the modal shows in the article
// view too. A public (/a/<token>) answer has no history id, so both stay hidden.
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

// installZmanim()'s dependency contract (plan.md §19 Phase 2, §19.9
// constraint 3): the module takes these eight inline classic-script globals
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
        setHebrewDate: window.setLocationHebrewDate,
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
    // Before the initial hydrate, so a ?conversation= link finds the UI ready.
    installConversationUI();
    // "Copy link" on stored answers: the AI modal footer (populateAiModal)
    // and the full-article view (renderArticle) each call .show per answer.
    // The copied link is public (/a/<token>, static/js/answer-share.js).
    const answerShare = createAnswerShare();
    window.ShelahAnswerLink = {
        modal: installAnswerLinkPlacement(document.getElementById("aiAnswerLink"), answerShare),
        article: installAnswerLinkPlacement(document.getElementById("readerAnswerLink"), answerShare),
    };

    installRouter((route) => window.hydrateRoute?.(route));
    window.hydrateRoute?.(readRoute(), { isInitial: true });
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initModules, { once: true });
} else {
    initModules();
}
