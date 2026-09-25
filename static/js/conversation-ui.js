// Multi-turn "Ask Sh'elah" conversation UI (design artifact "Conversation UI
// Placement Options"). Renders conversation-store.js into the markup in
// templates/index.html and wires every entry point to it:
//   - #convPanel      one panel, data-size = overlay | full | mini, and
//                     data-layout = desktop | mobile (on phones overlay is a
//                     bottom sheet and mini is the #convPip bar)
//   - #convHistoryPop recent conversations, shared by every history button:
//                     in the phone search tray and in the panel's header
//   - #topbarAskBtn   "Ask AI" beside the desktop search bar; opens the
//                     conversation as the mini corner widget, cross-fading in
//                     where it lives (a second press closes it again)
//   - #mobileTopbarAskBtn  the same mark in the phone top bar; opens the
//                     full-screen overlay
//   - #mobileSearchTray  rises above the tab bar while phone search is open:
//                     "Ask Sh'elah" (opens the conversation) + history
//   - #aiDiscoveryTip  one-time "there's an AI here" tip, anchored to the Ask
//                     button (desktop) or the phone Ask button; gone for
//                     good once the AI has been used or the tip dismissed
//
// Sources: every citation (inline under an answer, and the cards in the
// "Consulted N sources" drawer) opens the exact text in the reader and folds
// the conversation to mini so the text is what's on screen. The cards are the
// same .ai-source-box markup as the single-answer modal (source-cards.js).
//
// Routing (router.js): ?conversation=<id|new>&cv=<size>. Opening pushes a
// history entry, resizing replaces it, and once the first question creates
// the row, `new` is replaced by the real id so the URL is shareable. The
// classic script's hydrateRoute() hands every route to hydrate() below.
//
// Decision A1: the conversation UI saves nothing without a Clerk user, so a
// signed-out question goes to the legacy single-answer modal
// (handleAiSearch) with a sign-in hint instead.

import { createConversationStore, MESSAGE_STATUS, SOURCES_PHASE } from "./conversation-store.js";
import { isSignedIn } from "./conversation-entry.js";
import { pushRoute, readRoute } from "./router.js";
import { sourceBadgeHtml, externalLinksHtml, previewHtml } from "./source-cards.js";
import {
    normalizeSize,
    expandedSize,
    isModalSize,
    relativeTime,
    communityName,
    lockLine,
    noticeFor,
    snapCorner,
    logicalCorner,
    citationExcerpt,
    turnSignature,
} from "./conversation-ui-helpers.js";

const MOBILE_QUERY = "(max-width: 1023px)";
const CORNER_KEY = "shelah.conv.corner";
const AUTH_TIMEOUT_MS = 10000;
const LIST_STALE_MS = 30000;
const TOAST_MS = 8000;
const NEAR_BOTTOM_PX = 96;
const SWIPE_CLOSE_PX = 120;
const AI_USED_KEY = "shelah.ai.used";
const TIP_DISMISSED_KEY = "shelah.ai.tipDismissed";
const TIP_DELAY_MS = 1600;
const TIP_GAP_PX = 12;
// Menus that drop from the same top bars the tip points at. The tip never
// shows over one: it waits for it to close, and steps aside if one opens
// while the tip is up.
const TIP_BLOCKING_MENU_IDS = ["readerSettingsPanel", "settingsPanel", "userProfileMenu"];

const ui = {
    installed: false,
    open: false,
    size: "overlay",
    layout: "desktop",
    mode: null,
    signedIn: false,
    authResolved: false,
    awaitingOpenId: null,     // deep-linked id waiting for sign-in
    returnFocus: null,
    popTrigger: null,
    menuOpen: false,
    toast: null,              // { text, action } -- e.g. delete + Undo
    toastTimer: null,
    listLoadedAt: 0,
    renderQueued: false,
    turns: new Map(),         // message id -> { li, sig }
    threadKey: null,          // which thread the turn cache belongs to
    animateInserts: false,
    forceScroll: false,
    lastListRef: null,
    lastListItems: null,
    lastRouteSync: null,
    tipAnchor: null,
    tipTimer: null,
    tipDeferred: false,
    tipObserver: null,
    previews: new Map(),      // ref -> Promise<html> for the sources drawer
};

let store = null;
let els = null;
let resolveAuth = null;
const authReady = new Promise((resolve) => { resolveAuth = resolve; });

// ── small utilities ────────────────────────────────────────────────────

function lang() {
    try {
        if (typeof window.getCurrentLanguage === "function") return window.getCurrentLanguage();
    } catch (_err) { /* fall through */ }
    return document.documentElement.lang === "he" ? "he" : "en";
}

function tr(en, he) {
    return lang() === "he" ? he : en;
}

function motion() {
    return window.ShelahMotion || null;
}

function reducedMotion() {
    return Boolean(motion()?.isMotionReduced?.()) || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
}

function escapeText(value) {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

let iconPaths = {};
function icon(name, cls = "") {
    const d = iconPaths[name];
    if (!d) return "";
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"${cls ? ` class="${cls}"` : ""}><path d="${d}"/></svg>`;
}

function clerkConfigured() {
    return Boolean(window.APP_CLERK && window.APP_CLERK.publishableKey);
}

function communityOptions() {
    const select = document.getElementById("communityLensSelect");
    if (!select) return [];
    return [...select.options].map((o) => ({ value: o.value, en: o.dataset.en || o.value, he: o.dataset.he || o.value }));
}

function currentMode() {
    if (!ui.mode) ui.mode = window.appState?.prefs?.mode || "balanced";
    return ui.mode;
}

function effectiveLayout() {
    return window.matchMedia(MOBILE_QUERY).matches ? "mobile" : "desktop";
}

function readCorner() {
    try {
        const value = window.localStorage.getItem(CORNER_KEY);
        return ["br", "bl", "tr", "tl"].includes(value) ? value : "br";
    } catch (_err) {
        return "br";
    }
}

function readFlag(key) {
    try {
        return window.localStorage.getItem(key) === "1";
    } catch (_err) {
        return false;
    }
}

function writeFlag(key) {
    try {
        window.localStorage.setItem(key, "1");
    } catch (_err) {
        // Blocked storage: the tip may show again next visit, which is harmless.
    }
}

function saveCorner(corner) {
    try {
        window.localStorage.setItem(CORNER_KEY, corner);
    } catch (_err) {
        // Private mode / blocked storage: the corner just isn't remembered.
    }
}

// ── elements ───────────────────────────────────────────────────────────

function collectElements() {
    const $ = (id) => document.getElementById(id);
    const panel = $("convPanel");
    if (!panel) return null;
    return {
        panel,
        scrim: $("convScrim"),
        header: panel.querySelector(".conv-header"),
        grabber: panel.querySelector(".conv-grabber"),
        title: $("convTitle"),
        subtitle: $("convSubtitle"),
        historyBtn: $("convHistoryBtn"),
        chip: $("convMinhagChip"),
        chipLabel: $("convMinhagChipLabel"),
        menuBtn: $("convMenuBtn"),
        menu: $("convMenu"),
        menuLocked: $("convMenuMinhagLocked"),
        menuDraft: $("convMenuMinhagDraft"),
        minhagSelect: $("convMinhagSelect"),
        pinLabel: $("convPinLabel"),
        themeBtn: $("convThemeBtn"),
        expandBtn: $("convExpandBtn"),
        lockLine: $("convLockLine"),
        scroller: $("convScroller"),
        empty: $("convEmpty"),
        signedOutHint: $("convSignedOutHint"),
        loading: $("convLoading"),
        messages: $("convMessages"),
        jump: $("convJumpBtn"),
        notice: $("convNotice"),
        noticeText: $("convNoticeText"),
        noticeAction: $("convNoticeAction"),
        sources: $("convSources"),
        sourcesLabel: $("convSourcesLabel"),
        sourcesList: $("convSourcesList"),
        composer: $("convComposer"),
        input: $("convInput"),
        send: $("convSendBtn"),
        pip: $("convPip"),
        pipOpen: $("convPipOpen"),
        pipTitle: $("convPipTitle"),
        pipStatus: $("convPipStatus"),
        pop: $("convHistoryPop"),
        popSignedOut: document.querySelector("#convHistoryPop .conv-pop__signed-out"),
        tray: $("mobileSearchTray"),
        trayAsk: $("mobileTrayAskBtn"),
        trayHistory: $("mobileTrayHistoryBtn"),
        topAsk: $("topbarAskBtn"),
        phoneAsk: $("mobileTopbarAskBtn"),
        tip: $("aiDiscoveryTip"),
        tipTry: $("aiDiscoveryTipTry"),
        tipDismiss: $("aiDiscoveryTipDismiss"),
        aiSignInHint: $("aiSignInHint"),
        lists: [...document.querySelectorAll("[data-conv-list]")],
    };
}

// ── rendering ──────────────────────────────────────────────────────────

function scheduleRender() {
    if (ui.renderQueued) return;
    ui.renderQueued = true;
    queueMicrotask(() => {
        ui.renderQueued = false;
        render();
    });
}

function render() {
    if (!els || !store) return;
    const state = store.getState();
    const l = lang();
    const options = communityOptions();
    const conversation = state.conversation;
    const minhag = state.minhagLocked ? conversation?.minhag : state.draftMinhag;
    const name = communityName(minhag, l, options);
    const hasMessages = state.messages.length > 0;

    els.panel.dataset.hasThread = conversation ? "true" : "false";
    els.panel.dataset.sending = state.sending ? "true" : "false";
    els.pip.dataset.sending = state.sending ? "true" : "false";

    // Header.
    const title = conversation?.title || (hasMessages ? tr("New conversation", "שיחה חדשה") : tr("Ask Sh'elah", "שאל את ש׳אלה"));
    if (!els.title.querySelector("input")) els.title.textContent = title;
    els.subtitle.textContent = ui.layout === "mobile" ? tr(`${name} practice`, `מנהג ${name}`) : "";
    els.chipLabel.textContent = name;
    els.chip.dataset.locked = state.minhagLocked ? "true" : "false";
    els.chip.setAttribute("aria-label", state.minhagLocked
        ? tr(`Community: ${name}, locked for this conversation`, `קהילה: ${name}, נעולה לשיחה זו`)
        : tr(`Community: ${name}`, `קהילה: ${name}`));

    els.lockLine.textContent = state.minhagLocked
        ? lockLine(conversation?.minhag, l, options)
        : "";

    // Menu.
    els.menuLocked.textContent = state.minhagLocked
        ? tr(`${name} — locked for this conversation. Start a new conversation to change it.`,
            `${name} — נעולה לשיחה זו. התחל שיחה חדשה כדי לשנות.`)
        : "";
    els.menuDraft.classList.toggle("hidden", state.minhagLocked);
    if (!state.minhagLocked) syncMinhagSelect(options, state.draftMinhag, l);
    els.menu.querySelectorAll("[data-conv-mode]").forEach((btn) => {
        btn.setAttribute("aria-checked", btn.dataset.convMode === currentMode() ? "true" : "false");
    });
    els.pinLabel.textContent = conversation?.pinnedAt ? tr("Unpin", "בטל הצמדה") : tr("Pin", "הצמד");

    // Body: loading / empty / transcript.
    const loading = state.loadStatus === "loading" || (ui.awaitingOpenId && !ui.authResolved);
    els.loading.classList.toggle("hidden", !loading);
    els.empty.classList.toggle("hidden", loading || hasMessages || state.loadStatus === "error");
    els.signedOutHint.classList.toggle("hidden", !clerkConfigured() || ui.signedIn || !ui.authResolved);
    els.scroller.setAttribute("aria-busy", loading ? "true" : "false");
    renderTurns(state, l);

    // Notice: toast > load error > send error > awaiting sign-in.
    let notice = null;
    if (ui.toast) notice = { tone: "info", ...ui.toast };
    else if (state.loadStatus === "error") {
        notice = noticeFor(state.loadError, l);
        if (notice && !notice.action) notice.action = "retry-open";
    } else if (state.lastError) notice = noticeFor(state.lastError, l);
    else if (ui.awaitingOpenId && ui.authResolved && !ui.signedIn && clerkConfigured()) {
        notice = { tone: "info", text: tr("Sign in to open this conversation.", "התחבר כדי לפתוח את השיחה הזו."), action: "sign-in" };
    }
    renderNotice(notice);

    renderSources(state, l);

    // Composer.
    els.input.placeholder = hasMessages ? tr("Ask a follow-up…", "שאל שאלת המשך…") : tr("Ask a question…", "שאל שאלה…");
    syncSendDisabled();

    // Minimised bar.
    els.pipTitle.textContent = title;
    els.pipStatus.textContent = state.sending ? tr("Thinking…", "חושב…") : "";
    els.pipOpen.setAttribute("aria-label", tr(`Open conversation: ${title}`, `פתח שיחה: ${title}`));

    syncAskExpanded();

    renderLists(state, l);
    syncRoute(state);
}

function syncMinhagSelect(options, value, l) {
    const select = els.minhagSelect;
    const key = `${l}:${options.length}`;
    if (select.dataset.key !== key) {
        select.innerHTML = options
            .map((o) => `<option value="${escapeText(o.value)}">${escapeText(l === "he" ? o.he : o.en)}</option>`)
            .join("");
        select.dataset.key = key;
    }
    select.value = options.some((o) => o.value === value) ? value : "All";
}

function syncSendDisabled() {
    const state = store.getState();
    els.send.disabled = !els.input.value.trim() || state.sending || state.loadStatus === "loading";
}

function renderNotice(notice) {
    els.notice.classList.toggle("hidden", !notice);
    if (!notice) return;
    els.notice.dataset.tone = notice.tone || "warn";
    els.noticeText.textContent = notice.text;
    const labels = {
        "sign-in": tr("Sign in", "התחברות"),
        "retry-open": tr("Try again", "נסה שוב"),
        new: tr("Start a new one", "התחל שיחה חדשה"),
        undo: tr("Undo", "בטל"),
    };
    const label = labels[notice.action];
    els.noticeAction.classList.toggle("hidden", !label);
    els.noticeAction.dataset.action = notice.action || "";
    if (label) els.noticeAction.textContent = label;
}

function renderSources(state, l) {
    const { phase, citations } = state.sources;
    const searching = phase === SOURCES_PHASE.SEARCHING;
    const done = phase === SOURCES_PHASE.DONE && citations.length > 0;
    els.sources.classList.toggle("hidden", !(searching || done));
    els.sources.dataset.phase = searching ? "searching" : "done";
    if (searching) {
        els.sourcesLabel.textContent = tr("Consulting sources…", "מעיין במקורות…");
        els.sourcesList.innerHTML = "";
        els.sources.open = false;
        return;
    }
    if (!done) return;
    els.sourcesLabel.textContent = citations.length === 1
        ? tr("Consulted 1 source", "עיין במקור אחד")
        : tr(`Consulted ${citations.length} sources`, `עיין ב-${citations.length} מקורות`);
    const key = `${l}:${state.sources.messageId}`;
    if (els.sourcesList.dataset.key !== key) {
        els.sourcesList.innerHTML = citations.map((c) => sourceCardHtml(c, l)).join("");
        els.sourcesList.dataset.key = key;
        if (els.sources.open) animateSourceCards();
    }
}

// One consulted source, as the single-answer modal draws it: type badge, ref,
// where else to read it, and "Open in reader". The text preview is fetched
// only when its <details> is opened (loadPreview).
function sourceCardHtml(citation, l) {
    const ref = citation.ref || "";
    const excerpt = citationExcerpt(citation, l);
    if (!ref) {
        return excerpt ? `<li class="ai-source-box conv-source-card"><div class="ai-src-box-note" dir="auto">${escapeText(excerpt)}</div></li>` : "";
    }
    const open = `<a href="/?text=${encodeURIComponent(ref)}" class="src-open-link conv-source-card__open" data-cite-ref="${escapeText(ref)}">${escapeText(tr("Open in reader", "פתח בקורא"))} ↗</a>`;
    return `<li class="ai-source-box conv-source-card">
        <div class="ai-src-box-header">
            <span class="ai-src-box-id">${sourceBadgeHtml(ref, l)}<span class="ai-src-box-title" dir="auto" title="${escapeText(ref)}">${escapeText(ref)}</span></span>
        </div>
        <div class="conv-source-card__links">${open}${externalLinksHtml(ref)}</div>
        ${excerpt ? `<div class="ai-src-box-note" dir="auto">${escapeText(excerpt)}</div>` : ""}
        <details class="conv-source-card__preview" data-preview-ref="${escapeText(ref)}">
            <summary>${icon("caret-down", "conv-source-card__caret")}<span>${escapeText(tr("Preview the text", "הצג את הטקסט"))}</span></summary>
            <div class="conv-source-card__preview-body"></div>
        </details>
    </li>`;
}

function animateSourceCards() {
    const cards = [...els.sourcesList.querySelectorAll(".conv-source-card")];
    if (cards.length && !reducedMotion()) motion()?.staggerIn?.(cards, { staggerDelay: 0.04, y: 8 });
}

function fetchPreview(ref) {
    if (!ui.previews.has(ref)) {
        const request = fetch(`/api/text/${encodeURIComponent(ref)}?autotranslate=0`)
            .then((resp) => (resp.ok ? resp.json() : null))
            .then((payload) => (payload && !payload.error ? previewHtml(payload.lines) : ""))
            .catch(() => "");
        // A failed fetch is not cached, so reopening the preview retries it.
        request.then((html) => { if (!html) ui.previews.delete(ref); });
        ui.previews.set(ref, request);
    }
    return ui.previews.get(ref);
}

async function loadPreview(details) {
    const body = details.querySelector(".conv-source-card__preview-body");
    if (!body || !details.open || body.dataset.state === "done" || body.dataset.state === "loading") return;
    body.dataset.state = "loading";
    body.setAttribute("aria-busy", "true");
    body.innerHTML = '<div class="ai-src-box-body ai-cited-fetch-skeleton" aria-hidden="true"><div class="ai-src-skeleton-line ai-src-skeleton-line--wide"></div><div class="ai-src-skeleton-line ai-src-skeleton-line--medium"></div></div>';
    const html = await fetchPreview(details.dataset.previewRef);
    if (!body.isConnected) return;
    body.innerHTML = html || `<div class="ai-src-box-note">${escapeText(tr(
        "Preview unavailable for this source — use the links above to open it.",
        "תצוגה מקדימה אינה זמינה למקור זה — השתמש בקישורים למעלה כדי לפתוח אותו.",
    ))}</div>`;
    body.dataset.state = html ? "done" : "";
    body.setAttribute("aria-busy", "false");
    const M = motion();
    if (M?.springAnimate && !reducedMotion()) {
        M.springAnimate(body, { opacity: [0, 1], transform: ["translateY(6px)", "translateY(0px)"] }, M.APPLE_SPRING?.smooth);
    }
}

function answerHtml(content) {
    if (typeof window.renderAnswerMarkdown === "function") {
        try {
            return window.renderAnswerMarkdown(content);
        } catch (_err) { /* fall back to plain text */ }
    }
    return escapeText(content).replace(/\n/g, "<br>");
}

function statusRow({ tone = "", text, action = null, actionLabel = "", messageId = "" }) {
    const button = action
        ? `<button type="button" class="conv-turn__retry" data-turn-action="${action}" data-message-id="${escapeText(messageId)}">${escapeText(actionLabel)}</button>`
        : "";
    return `<p class="conv-turn__status${tone ? ` conv-turn__status--${tone}` : ""}">${tone === "error" ? icon("warning") : ""}<span>${escapeText(text)}</span>${button}</p>`;
}

function turnInnerHtml(message, index, messages, l) {
    if (message.role === "user") {
        let html = `<div class="conv-bubble" dir="auto">${escapeText(message.content)}</div>`;
        if (message.status === MESSAGE_STATUS.FAILED) {
            html += statusRow({ tone: "error", text: tr("Not sent.", "לא נשלח."), action: "retry", actionLabel: tr("Retry", "נסה שוב"), messageId: message.id });
        }
        return html;
    }
    const label = `<p class="conv-turn__label">${escapeText(tr("Sh'elah", "ש׳אלה"))}</p>`;
    switch (message.status) {
        case MESSAGE_STATUS.PENDING:
            return `${label}<span class="sr-only">${escapeText(tr("Sh'elah is answering…", "ש׳אלה עונה…"))}</span>`
                + '<div class="conv-turn__skel"></div><div class="conv-turn__skel"></div><div class="conv-turn__skel"></div>';
        case MESSAGE_STATUS.ERROR: {
            const isLast = index === messages.length - 1;
            return label + statusRow({
                tone: "error",
                text: tr("Sh'elah couldn't answer this one.", "ש׳אלה לא הצליחה לענות על זה."),
                action: isLast ? "retry" : null,
                actionLabel: tr("Retry", "נסה שוב"),
                messageId: message.id,
            });
        }
        case MESSAGE_STATUS.INCOMPLETE:
            return label + statusRow({
                text: tr("The answer is taking longer than usual.", "התשובה מתעכבת מהרגיל."),
                action: "check",
                actionLabel: tr("Check again", "בדוק שוב"),
                messageId: message.id,
            });
        default: {
            const cites = message.citations.length
                ? `<ol class="conv-cites" aria-label="${escapeText(tr("Sources", "מקורות"))}">${message.citations.map((c, i) => {
                    const excerpt = citationExcerpt(c, l);
                    const ref = c.ref
                        ? `<a href="/?text=${encodeURIComponent(c.ref)}" class="conv-cite__ref" data-cite-ref="${escapeText(c.ref)}" dir="auto">${escapeText(c.ref)}</a>`
                        : "";
                    const links = c.ref ? `<span class="conv-cite__links">${externalLinksHtml(c.ref)}</span>` : "";
                    const badge = c.ref ? sourceBadgeHtml(c.ref, l) : "";
                    return `<li class="conv-cite"><span class="conv-cite__num" aria-hidden="true">${i + 1}</span><span class="conv-cite__head">${badge}${ref}</span>${excerpt ? `<p class="conv-cite__excerpt" dir="auto">${escapeText(excerpt)}</p>` : ""}${links}</li>`;
                }).join("")}</ol>`
                : "";
            return `${label}<div class="conv-answer" dir="auto">${answerHtml(message.content)}</div>${cites}`;
        }
    }
}

function renderTurns(state, l) {
    const threadKey = `${state.conversation?.id || "draft"}:${l}`;
    const scroller = els.scroller;
    const nearBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < NEAR_BOTTOM_PX;
    if (threadKey !== ui.threadKey) {
        // New thread (or language): rebuild, and scroll to the latest turn.
        // A thread that got its id from the first question keeps its turns.
        const draftToSaved = ui.turns.size > 0 && ui.threadKey?.startsWith("draft:") && ui.threadKey.endsWith(`:${l}`);
        if (!draftToSaved) {
            ui.turns.clear();
            els.messages.innerHTML = "";
            ui.animateInserts = false;
            ui.forceScroll = true;
        }
        ui.threadKey = threadKey;
    }

    const seen = new Set();
    const nextKeys = new Set(state.messages.map((message) => String(message.id)));
    let previous = null;
    state.messages.forEach((message, index) => {
        const key = String(message.id);
        seen.add(key);
        const sig = `${turnSignature(message)}:${index === state.messages.length - 1}`;
        let entry = ui.turns.get(key);
        // A saved turn replacing its optimistic placeholder (local id -> server
        // id) takes over the placeholder's row, so it doesn't animate in twice.
        const inPlace = previous ? previous.nextSibling : els.messages.firstChild;
        const placeholderKey = inPlace?.dataset?.turnKey;
        if (!entry && placeholderKey && !nextKeys.has(placeholderKey) && ui.turns.has(placeholderKey)) {
            entry = ui.turns.get(placeholderKey);
            ui.turns.delete(placeholderKey);
            ui.turns.set(key, entry);
            entry.li.dataset.turnKey = key;
        }
        if (!entry) {
            const li = document.createElement("li");
            li.dataset.turnKey = key;
            entry = { li, sig: null };
            ui.turns.set(key, entry);
            if (ui.animateInserts && !reducedMotion()) {
                li.classList.add("conv-turn--enter");
                li.addEventListener("animationend", () => li.classList.remove("conv-turn--enter"), { once: true });
            }
        }
        if (entry.sig !== sig) {
            const li = entry.li;
            li.className = `conv-turn conv-turn--${message.role}${message.status === MESSAGE_STATUS.FAILED ? " conv-turn--failed" : ""}${li.classList.contains("conv-turn--enter") ? " conv-turn--enter" : ""}`;
            li.setAttribute("aria-busy", message.status === MESSAGE_STATUS.PENDING ? "true" : "false");
            li.innerHTML = turnInnerHtml(message, index, state.messages, l);
            entry.sig = sig;
        }
        const expectedPosition = previous ? previous.nextSibling : els.messages.firstChild;
        if (entry.li !== expectedPosition) els.messages.insertBefore(entry.li, expectedPosition);
        previous = entry.li;
    });
    for (const [key, entry] of ui.turns) {
        if (!seen.has(key)) {
            entry.li.remove();
            ui.turns.delete(key);
        }
    }
    // Only turns that arrive after a thread is on screen animate in; a
    // thread being (re)loaded renders all at once.
    ui.animateInserts = state.loadStatus !== "loading";

    if (ui.forceScroll || nearBottom) {
        ui.forceScroll = false;
        requestAnimationFrame(() => scrollToLatest(false));
    }
    updateJumpButton();
}

function scrollToLatest(smooth = true) {
    const scroller = els.scroller;
    scroller.scrollTo({ top: scroller.scrollHeight, behavior: smooth && !reducedMotion() ? "smooth" : "auto" });
}

function updateJumpButton() {
    const scroller = els.scroller;
    const away = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight > NEAR_BOTTOM_PX * 2;
    els.jump.classList.toggle("hidden", !(away && store.getState().messages.length > 0));
}

function renderLists(state, l) {
    const list = state.list;
    const listRef = `${l}:${list.status}:${state.conversation?.id || ""}:${ui.signedIn}`;
    if (ui.lastListRef === listRef && ui.lastListItems === list.items) return;
    ui.lastListRef = listRef;
    ui.lastListItems = list.items;

    let html;
    if (!ui.signedIn) {
        html = "";
    } else if (list.status === "loading" && list.items.length === 0) {
        html = '<li aria-hidden="true"><div class="conv-list__skel"></div></li>'.repeat(3);
    } else if (list.status === "error" && list.items.length === 0) {
        html = `<li class="conv-list__error">${escapeText(tr("Couldn't load conversations.", "לא ניתן לטעון שיחות."))} <button type="button" class="conv-link-btn" data-conv-action="reload-list">${escapeText(tr("Try again", "נסה שוב"))}</button></li>`;
    } else if (list.items.length === 0) {
        html = `<li class="conv-list__empty">${escapeText(tr("No conversations yet.", "אין עדיין שיחות."))}</li>`;
    } else {
        const options = communityOptions();
        const currentId = state.conversation?.id;
        html = list.items.map((item) => {
            const meta = [communityName(item.minhag, l, options), relativeTime(item.updatedAt || item.createdAt, { lang: l })]
                .filter(Boolean).join(" · ");
            return `<li><button type="button" class="conv-list__item" data-conv-open="${escapeText(item.id)}" aria-current="${item.id === currentId ? "true" : "false"}">`
                + `<span class="conv-list__text"><span class="conv-list__title" dir="auto">${escapeText(item.title || tr("Untitled conversation", "שיחה ללא שם"))}</span>`
                + `<span class="conv-list__meta">${escapeText(meta)}</span></span>`
                + `${item.pinnedAt ? `${icon("pin", "conv-list__pin")}<span class="sr-only">${escapeText(tr("Pinned", "מוצמד"))}</span>` : ""}</button></li>`;
        }).join("");
    }
    els.lists.forEach((ul) => { ul.innerHTML = html; });
    els.popSignedOut?.classList.toggle("hidden", ui.signedIn || !clerkConfigured());
    const popList = els.pop.querySelector(".conv-list");
    popList?.classList.toggle("hidden", !ui.signedIn);
}

// Keep ?conversation= in step with the store: `new` becomes the real id once
// the first question creates the row, and a deleted open thread falls back
// to `new`. Only while signed in -- a deep link waiting for sign-in must
// keep its id.
function syncRoute(state) {
    if (!ui.open || !ui.signedIn || ui.awaitingOpenId) return;
    let desired = null;
    if (state.conversation) desired = state.conversation.id;
    else if (state.loadStatus === "ready") desired = "new";
    if (!desired) return;
    const route = readRoute();
    if (route.conversation === desired) return;
    pushRoute({ conversation: desired, cv: ui.size }, { replace: true });
}

// ── open / close / size ────────────────────────────────────────────────

function lockBody() {
    const modal = ui.open && isModalSize(ui.size) && !(ui.layout === "mobile" && ui.size === "mini");
    document.body.classList.toggle("conv-modal-open", modal);
}

// Desktop mini/overlay: motion.js's "window" cross-fade, the same as every
// other dialog window. It appears where it lives rather than growing out of
// the Ask mark, which read as the button flying across the screen.
function panelPreset() {
    if (ui.layout === "mobile") return ui.size === "full" ? "fade" : "drawer";
    return ui.size === "full" ? "fade" : "window";
}

function applyAttributes() {
    els.panel.dataset.size = ui.size;
    els.panel.dataset.layout = ui.layout;
    const rtl = document.documentElement.dir === "rtl";
    els.panel.dataset.corner = logicalCorner(readCorner(), rtl);
    const modal = isModalSize(ui.size);
    els.panel.setAttribute("aria-modal", modal ? "true" : "false");
    els.panel.setAttribute("role", modal ? "dialog" : "region");
    if (els.expandBtn) {
        const toFull = expandedSize(ui.size) === "full";
        const label = toFull ? tr("Open full page", "פתח בעמוד מלא") : tr("Expand", "הרחב");
        els.expandBtn.setAttribute("aria-label", label);
        els.expandBtn.title = label;
    }
}

function show(el, preset, origin = null) {
    const M = motion();
    if (M?.present) return M.present(el, { preset, origin });
    el.classList.remove("hidden");
    return Promise.resolve();
}

function hide(el, preset) {
    const M = motion();
    if (M?.dismiss) return M.dismiss(el, { preset });
    el.classList.add("hidden");
    return Promise.resolve();
}

// FLIP between two sizes: measure, switch, and spring from the old box.
function flipResize(mutate) {
    const el = els.panel;
    const first = el.getBoundingClientRect();
    mutate();
    const last = el.getBoundingClientRect();
    const M = motion();
    if (!M?.springAnimate || reducedMotion() || !first.width || !last.width || el.classList.contains("hidden")) return;
    const dx = first.left - last.left;
    const dy = first.top - last.top;
    const sx = first.width / last.width;
    const sy = first.height / last.height;
    el.style.transformOrigin = "top left";
    const controls = M.springAnimate(el, {
        transform: [`translate(${dx}px, ${dy}px) scale(${sx}, ${sy})`, "translate(0px, 0px) scale(1, 1)"],
    }, M.APPLE_SPRING?.snappy || { stiffness: 158, damping: 21, mass: 1 });
    Promise.resolve(controls).finally(() => {
        el.style.removeProperty("transform");
        el.style.removeProperty("transform-origin");
    });
}

function applyVisibility({ previousSize = null, origin = null, opening = false } = {}) {
    const mobileMini = ui.layout === "mobile" && ui.size === "mini";
    const wantScrim = ui.open && ui.size === "overlay";
    const wantPanel = ui.open && !mobileMini;
    const wantPip = ui.open && mobileMini;

    if (wantScrim) show(els.scrim, "fade");
    else hide(els.scrim, "fade");

    const panelHidden = els.panel.classList.contains("hidden") || els.panel.classList.contains("is-hiding");
    if (wantPanel && (opening || panelHidden)) {
        applyAttributes();
        show(els.panel, panelPreset(), origin);
    } else if (wantPanel && previousSize && previousSize !== ui.size) {
        flipResize(applyAttributes);
    } else if (wantPanel) {
        applyAttributes();
    } else {
        hide(els.panel, ui.layout === "mobile" ? "drawer" : panelPreset());
    }

    if (wantPip) show(els.pip, "drawer");
    else hide(els.pip, ui.open ? "fade" : "drawer");
    lockBody();
}

function focusInside() {
    if (ui.layout === "mobile" && ui.size === "mini") {
        els.pipOpen.focus({ preventScroll: true });
        return;
    }
    // Phones: focusing the textarea would pop the keyboard over the sheet
    // before the user has read anything, so focus the panel itself.
    if (ui.layout === "mobile") els.panel.focus({ preventScroll: true });
    else els.input.focus({ preventScroll: true });
}

function openPanel({ size = null, origin = null, fromRoute = false, routeId = null } = {}) {
    const nextSize = normalizeSize(size || (ui.open ? ui.size : "overlay"));
    const wasOpen = ui.open;
    const previousSize = ui.size;
    ui.layout = effectiveLayout();
    ui.size = nextSize;
    markAiUsed();
    if (!wasOpen) {
        ui.open = true;
        ui.returnFocus = origin || (document.activeElement instanceof HTMLElement ? document.activeElement : null);
        closeHistoryPop();
        applyVisibility({ origin, opening: true });
        requestAnimationFrame(focusInside);
    } else {
        applyVisibility({ previousSize });
    }
    if (!fromRoute) {
        const state = store.getState();
        const id = routeId || state.conversation?.id || "new";
        pushRoute({ conversation: id, cv: ui.size }, { replace: wasOpen });
    }
    scheduleRender();
}

// The Ask buttons' aria-expanded; also set on close, which doesn't render.
function syncAskExpanded() {
    const value = ui.open ? "true" : "false";
    els.topAsk?.setAttribute("aria-expanded", value);
    els.phoneAsk?.setAttribute("aria-expanded", value);
}

function closePanel({ fromRoute = false } = {}) {
    if (!ui.open) return;
    ui.open = false;
    syncAskExpanded();
    closeMenu();
    closeHistoryPop();
    hide(els.panel, panelPreset());
    hide(els.scrim, "fade");
    hide(els.pip, "drawer");
    lockBody();
    const target = ui.returnFocus;
    ui.returnFocus = null;
    if (target && document.contains(target) && target.offsetParent !== null) target.focus({ preventScroll: true });
    if (!fromRoute) pushRoute({ conversation: null, cv: null });
}

function setSize(size, { fromRoute = false } = {}) {
    const next = normalizeSize(size);
    if (!ui.open) {
        openPanel({ size: next, fromRoute });
        return;
    }
    if (next === ui.size) return;
    const previousSize = ui.size;
    ui.size = next;
    closeMenu();
    applyVisibility({ previousSize });
    if (!fromRoute) pushRoute({ cv: next }, { replace: true });
    // Crossing between the sheet and the phone's bar moves focus with it.
    if (ui.layout === "mobile" && (previousSize === "mini" || next === "mini")) requestAnimationFrame(focusInside);
    scheduleRender();
}

// ── public entry points ────────────────────────────────────────────────

async function waitForAuth() {
    if (ui.authResolved) return;
    await Promise.race([authReady, new Promise((r) => setTimeout(r, AUTH_TIMEOUT_MS))]);
}

function syncSignInHint() {
    els.aiSignInHint?.classList.toggle("hidden", ui.signedIn || !clerkConfigured());
}

// Decision A1: a signed-out question gets the legacy single-answer modal.
function askLegacy(question) {
    syncSignInHint();
    closePanel();
    if (typeof window.handleAiSearch === "function") void window.handleAiSearch(question);
}

// Open (or resume) the conversation UI. `fresh` starts a new thread first;
// `question` is sent straight away.
async function openConversation({ id = null, fresh = false, question = "", trigger = null, size = null } = {}) {
    if (!store) return;
    const text = String(question || "").trim();
    if (text) {
        await waitForAuth();
        if (!ui.signedIn) {
            askLegacy(text);
            return;
        }
    }
    const state = store.getState();
    if (id) {
        ui.awaitingOpenId = null;
        if (state.conversation?.id !== id) void store.open(id);
        openPanel({ size, origin: trigger, routeId: id });
        return;
    }
    if (fresh || (text && (state.conversation || state.messages.length))) {
        if (state.conversation || state.messages.length || state.loadStatus !== "ready") {
            store.startNew({ minhag: window.appState?.prefs?.community });
        }
        ui.awaitingOpenId = null;
    }
    openPanel({ size, origin: trigger });
    if (text) {
        ui.forceScroll = true;
        void store.send(text, { mode: currentMode(), language: lang() });
    }
}

// Router: `route` is readRoute()'s shape. Called from the classic script's
// hydrateRoute() for the initial URL and on every back/forward.
function hydrate(route = {}, { isInitial = false } = {}) {
    if (!store) return;
    const id = route.conversation;
    if (!id) {
        ui.awaitingOpenId = null;
        closePanel({ fromRoute: true });
        return;
    }
    const size = normalizeSize(route.cv);
    if (id === "new") {
        const state = store.getState();
        if (state.conversation) store.startNew({ minhag: window.appState?.prefs?.community });
        ui.awaitingOpenId = null;
    } else if (store.getState().conversation?.id !== id) {
        if (ui.authResolved && ui.signedIn) {
            ui.awaitingOpenId = null;
            void store.open(id);
        } else {
            ui.awaitingOpenId = id;
        }
    }
    if (ui.open) setSize(size, { fromRoute: true });
    else openPanel({ size, fromRoute: true });
    if (isInitial) scheduleRender();
}

// ── history popover ────────────────────────────────────────────────────

function positionPop(trigger) {
    const pop = els.pop;
    const margin = 8;
    const rect = trigger.getBoundingClientRect();
    const width = pop.offsetWidth || 320;
    const height = pop.offsetHeight || 320;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const rtl = document.documentElement.dir === "rtl";
    let left = rtl ? rect.left : rect.right - width;
    left = Math.min(Math.max(margin, left), vw - width - margin);
    let top = rect.bottom + margin;
    if (top + height > vh - margin) top = Math.max(margin, rect.top - height - margin);
    pop.style.left = `${Math.round(left)}px`;
    pop.style.top = `${Math.round(top)}px`;
}

function setTriggerExpanded(trigger, expanded) {
    document.querySelectorAll(".conv-history-trigger").forEach((btn) => {
        btn.setAttribute("aria-expanded", expanded && btn === trigger ? "true" : "false");
    });
}

function openHistoryPop(trigger) {
    if (!trigger) return;
    if (!els.pop.classList.contains("hidden") && ui.popTrigger === trigger) {
        closeHistoryPop();
        return;
    }
    closeMenu();
    ui.popTrigger = trigger;
    maybeLoadList();
    render();
    els.pop.classList.remove("hidden");
    positionPop(trigger);
    els.pop.classList.add("hidden");
    show(els.pop, "popover", trigger);
    setTriggerExpanded(trigger, true);
    requestAnimationFrame(() => {
        const first = els.pop.querySelector("[aria-current='true'], .conv-list__item, .conv-pop__new");
        first?.focus({ preventScroll: true });
    });
}

function closeHistoryPop({ restoreFocus = false } = {}) {
    if (!els || els.pop.classList.contains("hidden")) return;
    const trigger = ui.popTrigger;
    ui.popTrigger = null;
    hide(els.pop, "popover");
    setTriggerExpanded(null, false);
    if (restoreFocus) trigger?.focus({ preventScroll: true });
}

function maybeLoadList(force = false) {
    if (!ui.signedIn) return;
    if (!force && Date.now() - ui.listLoadedAt < LIST_STALE_MS && store.getState().list.status === "ready") return;
    ui.listLoadedAt = Date.now();
    void store.loadList({ limit: 30 });
}

// ── settings menu ──────────────────────────────────────────────────────

function openMenu(trigger) {
    if (ui.menuOpen) {
        closeMenu();
        return;
    }
    closeHistoryPop();
    ui.menuOpen = true;
    render();
    show(els.menu, "popover", trigger);
    [els.menuBtn, els.chip].forEach((btn) => btn.setAttribute("aria-expanded", btn === trigger ? "true" : "false"));
    requestAnimationFrame(() => {
        const first = els.menu.querySelector("select:not([disabled]), [aria-checked='true'], button");
        first?.focus({ preventScroll: true });
    });
}

function closeMenu({ restoreFocus = false } = {}) {
    if (!els || !ui.menuOpen) return;
    ui.menuOpen = false;
    hide(els.menu, "popover");
    const trigger = [els.menuBtn, els.chip].find((btn) => btn.getAttribute("aria-expanded") === "true");
    [els.menuBtn, els.chip].forEach((btn) => btn.setAttribute("aria-expanded", "false"));
    if (restoreFocus) trigger?.focus({ preventScroll: true });
}

// ── thread actions ─────────────────────────────────────────────────────

function showToast(text, action) {
    clearTimeout(ui.toastTimer);
    ui.toast = { text, action };
    ui.toastTimer = setTimeout(() => {
        ui.toast = null;
        scheduleRender();
    }, TOAST_MS);
    scheduleRender();
}

function clearToast() {
    clearTimeout(ui.toastTimer);
    ui.toast = null;
}

function beginRename() {
    const conversation = store.getState().conversation;
    if (!conversation || els.title.querySelector("input")) return;
    closeMenu();
    const input = document.createElement("input");
    input.type = "text";
    input.className = "conv-title-input";
    input.maxLength = 120;
    input.dir = "auto";
    input.value = conversation.title || "";
    input.setAttribute("aria-label", tr("Conversation name", "שם השיחה"));
    els.title.textContent = "";
    els.title.appendChild(input);
    input.focus();
    input.select();
    let done = false;
    const finish = (commit) => {
        if (done) return;
        done = true;
        const value = input.value.trim();
        input.remove();
        els.title.textContent = conversation.title;
        if (commit && value && value !== conversation.title) void store.rename(conversation.id, value);
        scheduleRender();
        els.menuBtn.focus({ preventScroll: true });
    };
    input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            event.preventDefault();
            finish(true);
        } else if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            finish(false);
        }
    });
    input.addEventListener("blur", () => finish(true));
}

async function deleteCurrent() {
    const conversation = store.getState().conversation;
    if (!conversation) return;
    closeMenu();
    if (await store.remove(conversation.id)) {
        showToast(tr("Conversation deleted.", "השיחה נמחקה."), "undo");
    }
}

function openCitation(ref) {
    if (!ref) return;
    // Keep the conversation on screen but out of the way of the text: the
    // corner widget on desktop, the bar above the tabs on phones.
    if (ui.size !== "mini") setSize("mini");
    // skipNavigationGrid: a cited ref is already specific ("Berakhot 2a"),
    // so go straight to the text rather than a chapter picker.
    if (typeof window.readText === "function") void window.readText(ref, { skipNavigationGrid: true });
}

// ── topbar Ask button + discovery tip ─────────────────────────────────

function onTopAsk() {
    if (!ui.open) {
        void openConversation({ size: "mini", trigger: els.topAsk });
    } else if (ui.size !== "mini") {
        setSize("mini");
    } else {
        closePanel();
    }
}

// Phone top bar: opens the full-screen overlay; a second tap restores a
// minimised bar, or closes the conversation.
function onPhoneAsk() {
    if (!ui.open) {
        void openConversation({ trigger: els.phoneAsk });
    } else if (ui.size === "mini") {
        setSize("overlay");
    } else {
        closePanel();
    }
}

function markAiUsed() {
    writeFlag(AI_USED_KEY);
    window.clearTimeout(ui.tipTimer);
    ui.tipDeferred = false;
    hideTip();
}

function tipAnchor() {
    if (effectiveLayout() === "desktop") return els.topAsk;
    return els.phoneAsk || document.querySelector('#mobileBottomTabs [data-mobile-tab="search"]');
}

function positionTip() {
    const anchor = ui.tipAnchor;
    if (!anchor || !els.tip) return;
    const a = anchor.getBoundingClientRect();
    const tip = els.tip;
    const width = tip.offsetWidth;
    const height = tip.offsetHeight;
    const margin = 16;
    // Below a top-bar anchor, above a bottom-tab one.
    const below = a.top < window.innerHeight / 2;
    const center = a.left + a.width / 2;
    const left = Math.min(Math.max(margin, center - width / 2), window.innerWidth - width - margin);
    const top = below ? a.bottom + TIP_GAP_PX : a.top - height - TIP_GAP_PX;
    tip.dataset.placement = below ? "below" : "above";
    tip.style.left = `${Math.round(left)}px`;
    tip.style.top = `${Math.round(Math.max(margin, top))}px`;
    tip.style.setProperty("--ai-tip-arrow-x", `${Math.round(center - left)}px`);
}

function maybeShowTip() {
    if (!els.tip || readFlag(AI_USED_KEY) || readFlag(TIP_DISMISSED_KEY)) return;
    window.clearTimeout(ui.tipTimer);
    ui.tipTimer = window.setTimeout(() => {
        // Only on a calm screen: never over an open panel, modal, search or reader.
        const busy = ui.open
            || openTipBlockingMenu()
            || document.body.classList.contains("mobile-search-open")
            || document.body.classList.contains("conv-modal-open")
            || !document.getElementById("aiAssistantModal")?.classList.contains("hidden");
        const anchor = tipAnchor();
        if (busy || !anchor || anchor.offsetParent === null || readFlag(AI_USED_KEY) || readFlag(TIP_DISMISSED_KEY)) return;
        ui.tipAnchor = anchor;
        anchor.classList.add("ai-attention");
        els.tip.classList.remove("hidden");
        positionTip();
        els.tip.classList.add("hidden");
        void show(els.tip, "popover", anchor);
        followTipAnchor(anchor);
    }, TIP_DELAY_MS);
}

function openTipBlockingMenu() {
    return TIP_BLOCKING_MENU_IDS.some((id) => {
        const menu = document.getElementById(id);
        // Leaving (is-hiding) counts as closed, as index.html's isMenuClosed.
        return Boolean(menu) && !menu.classList.contains("hidden") && !menu.classList.contains("is-hiding");
    });
}

// A menu opening hides the tip for now (not for good: it comes back once
// every menu is closed again, unless the tip was dismissed meanwhile).
function watchTipBlockingMenus() {
    const menus = TIP_BLOCKING_MENU_IDS.map((id) => document.getElementById(id)).filter(Boolean);
    if (!menus.length) return;
    const observer = new MutationObserver(() => {
        if (openTipBlockingMenu()) {
            if (ui.tipAnchor) {
                ui.tipDeferred = true;
                hideTip();
            }
        } else if (ui.tipDeferred) {
            ui.tipDeferred = false;
            maybeShowTip();
        }
    });
    menus.forEach((menu) => observer.observe(menu, { attributes: true, attributeFilter: ["class"] }));
}

// The bars keep filling in after load (the Hebrew date, the account button),
// which slides the anchor sideways: re-seat the tip whenever anything in the
// anchor's bar changes size, for as long as the tip is up.
function followTipAnchor(anchor) {
    if (typeof ResizeObserver !== "function") return;
    const bar = anchor.closest("header, nav, #mobileBottomTabs") || anchor.parentElement;
    ui.tipObserver = new ResizeObserver(() => positionTip());
    [bar, ...bar.querySelectorAll("*")].forEach((el) => ui.tipObserver.observe(el));
}

function hideTip() {
    ui.tipObserver?.disconnect();
    ui.tipObserver = null;
    ui.tipAnchor?.classList.remove("ai-attention");
    ui.tipAnchor = null;
    if (els.tip && !els.tip.classList.contains("hidden")) void hide(els.tip, "popover");
}

function dismissTip() {
    writeFlag(TIP_DISMISSED_KEY);
    ui.tipDeferred = false;
    const anchor = ui.tipAnchor;
    hideTip();
    if (anchor && els.tip.contains(document.activeElement)) anchor.focus({ preventScroll: true });
}

function tryFromTip() {
    const anchor = ui.tipAnchor;
    markAiUsed();
    if (effectiveLayout() === "desktop") void openConversation({ size: "mini", trigger: anchor || els.topAsk });
    else void openConversation({ trigger: anchor });
}

async function submitComposer() {
    const text = els.input.value.trim();
    if (!text) return;
    const state = store.getState();
    if (state.sending || state.loadStatus === "loading") return;
    await waitForAuth();
    if (!ui.signedIn) {
        els.input.value = "";
        autosize();
        askLegacy(text);
        return;
    }
    els.input.value = "";
    autosize();
    clearToast();
    ui.forceScroll = true;
    // A refused question stays in the transcript as FAILED with a Retry.
    await store.send(text, { mode: currentMode(), language: lang() });
}

function autosize() {
    const input = els.input;
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
    syncSendDisabled();
}

// ── gestures ───────────────────────────────────────────────────────────

// Desktop mini widget: drag by its header, snap to the nearest corner.
function installMiniDrag() {
    let drag = null;
    els.header.addEventListener("pointerdown", (event) => {
        if (ui.layout !== "desktop" || ui.size !== "mini" || event.button !== 0) return;
        if (event.target.closest("button, input, select, a")) return;
        const rect = els.panel.getBoundingClientRect();
        drag = { id: event.pointerId, x: event.clientX, y: event.clientY, rect };
        els.header.setPointerCapture(event.pointerId);
        els.panel.classList.add("is-dragging");
    });
    els.header.addEventListener("pointermove", (event) => {
        if (!drag || event.pointerId !== drag.id) return;
        const dx = event.clientX - drag.x;
        const dy = event.clientY - drag.y;
        els.panel.style.transform = `translate(${dx}px, ${dy}px)`;
    });
    const end = (event) => {
        if (!drag || event.pointerId !== drag.id) return;
        const dx = event.clientX - drag.x;
        const dy = event.clientY - drag.y;
        const center = { x: drag.rect.left + drag.rect.width / 2 + dx, y: drag.rect.top + drag.rect.height / 2 + dy };
        drag = null;
        els.panel.classList.remove("is-dragging");
        const physical = snapCorner(center, { width: window.innerWidth, height: window.innerHeight });
        const rtl = document.documentElement.dir === "rtl";
        // Stored in logical terms so the widget stays on the "end" side after a language switch.
        saveCorner(logicalCorner(physical, rtl));
        flipResize(() => {
            els.panel.style.removeProperty("transform");
            els.panel.dataset.corner = logicalCorner(readCorner(), rtl);
        });
        if (Math.hypot(dx, dy) < 4) els.panel.style.removeProperty("transform");
    };
    els.header.addEventListener("pointerup", end);
    els.header.addEventListener("pointercancel", end);
}

// Phone sheet: swipe down on the grabber/header to minimise to the bar.
function installSheetSwipe() {
    let swipe = null;
    const start = (event) => {
        if (ui.layout !== "mobile" || ui.size !== "overlay") return;
        if (event.target.closest("button, input, select, a")) return;
        swipe = { id: event.pointerId, y: event.clientY, t: performance.now(), dy: 0 };
        event.currentTarget.setPointerCapture(event.pointerId);
    };
    const move = (event) => {
        if (!swipe || event.pointerId !== swipe.id) return;
        swipe.dy = Math.max(0, event.clientY - swipe.y);
        els.panel.style.transform = `translateY(${swipe.dy}px)`;
    };
    const end = (event) => {
        if (!swipe || event.pointerId !== swipe.id) return;
        const { dy, t } = swipe;
        swipe = null;
        const velocity = dy / Math.max(1, performance.now() - t); // px/ms
        if (dy > SWIPE_CLOSE_PX || velocity > 0.8) {
            els.panel.style.removeProperty("transform");
            setSize("mini");
            return;
        }
        const M = motion();
        const controls = M?.springAnimate?.(els.panel, { transform: [`translateY(${dy}px)`, "translateY(0px)"] }, M.APPLE_SPRING?.snappy);
        if (controls) Promise.resolve(controls).finally(() => els.panel.style.removeProperty("transform"));
        else els.panel.style.removeProperty("transform");
    };
    [els.grabber, els.header].forEach((el) => {
        el.addEventListener("pointerdown", start);
        el.addEventListener("pointermove", move);
        el.addEventListener("pointerup", end);
        el.addEventListener("pointercancel", end);
    });
}

// ── events ─────────────────────────────────────────────────────────────

function focusables(root) {
    return [...root.querySelectorAll("a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), summary, [tabindex]:not([tabindex='-1'])")]
        .filter((el) => el.offsetParent !== null || el === document.activeElement);
}

function onKeydown(event) {
    if (!els) return;
    if (event.key === "Escape") {
        if (!els.pop.classList.contains("hidden")) {
            event.preventDefault();
            closeHistoryPop({ restoreFocus: true });
            return;
        }
        if (ui.menuOpen) {
            event.preventDefault();
            closeMenu({ restoreFocus: true });
            return;
        }
        if (ui.open && isModalSize(ui.size) && !(ui.layout === "mobile" && ui.size === "mini")) {
            event.preventDefault();
            closePanel();
        }
        return;
    }
    if (event.key !== "Tab" || !ui.open || !isModalSize(ui.size) || (ui.layout === "mobile" && ui.size === "mini")) return;
    if (!els.pop.classList.contains("hidden")) return; // popover owns focus
    const items = focusables(els.panel);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (!els.panel.contains(document.activeElement)) {
        event.preventDefault();
        first.focus();
    } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
    }
}

function onDocumentClick(event) {
    const target = event.target;
    if (!(target instanceof Element)) return;

    // Outside clicks close the popover / menu.
    if (!els.pop.classList.contains("hidden") && !els.pop.contains(target) && !target.closest(".conv-history-trigger")) {
        closeHistoryPop();
    }
    if (ui.menuOpen && !els.menu.contains(target) && !target.closest("#convMenuBtn, #convMinhagChip")) {
        closeMenu();
    }

    const trigger = target.closest(".conv-history-trigger");
    if (trigger) {
        event.preventDefault();
        openHistoryPop(trigger);
        return;
    }

    const openBtn = target.closest("[data-conv-open]");
    if (openBtn) {
        const id = openBtn.dataset.convOpen;
        const fromPop = els.pop.contains(openBtn);
        closeHistoryPop();
        if (fromPop && document.body.classList.contains("mobile-search-open")) window.setMobileSearchExpanded?.(false);
        void openConversation({ id, trigger: fromPop ? ui.popTrigger : openBtn });
        return;
    }

    const sizeBtn = target.closest("[data-conv-size]");
    if (sizeBtn && (els.panel.contains(sizeBtn) || els.pip.contains(sizeBtn))) {
        const wanted = sizeBtn.dataset.convSize;
        setSize(wanted === "expand" ? expandedSize(ui.size) : wanted);
        return;
    }

    const actionBtn = target.closest("[data-conv-action]");
    if (actionBtn) {
        handleAction(actionBtn.dataset.convAction, actionBtn);
        return;
    }

    const modeBtn = target.closest("[data-conv-mode]");
    if (modeBtn && els.menu.contains(modeBtn)) {
        ui.mode = modeBtn.dataset.convMode;
        render();
        return;
    }

    const turnBtn = target.closest("[data-turn-action]");
    if (turnBtn && els.messages.contains(turnBtn)) {
        if (turnBtn.dataset.turnAction === "check") void store.refresh();
        else void store.retry(turnBtn.dataset.messageId, { mode: currentMode(), language: lang() });
        return;
    }

    const cite = target.closest("[data-cite-ref]");
    if (cite && (els.messages.contains(cite) || els.sourcesList.contains(cite))) {
        // Modified clicks keep the browser's own behaviour (new tab via ?text=).
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
        event.preventDefault();
        openCitation(cite.dataset.citeRef);
    }
}

function handleAction(action, button) {
    switch (action) {
        case "new":
            closeHistoryPop();
            closeMenu();
            if (document.body.classList.contains("mobile-search-open")) window.setMobileSearchExpanded?.(false);
            void openConversation({ fresh: true, trigger: button });
            break;
        case "close":
            closePanel();
            break;
        case "sign-in":
            closeHistoryPop();
            if (typeof window.handleSignIn === "function") void window.handleSignIn();
            break;
        case "rename":
            beginRename();
            break;
        case "pin": {
            const conversation = store.getState().conversation;
            closeMenu();
            if (conversation) void store.setPinned(conversation.id, !conversation.pinnedAt);
            break;
        }
        case "delete":
            void deleteCurrent();
            break;
        case "reload-list":
            maybeLoadList(true);
            break;
        default:
            break;
    }
}

function onNoticeAction() {
    const action = els.noticeAction.dataset.action;
    if (action === "undo") {
        clearToast();
        void store.undoRemove();
    } else if (action === "sign-in") {
        if (typeof window.handleSignIn === "function") void window.handleSignIn();
    } else if (action === "retry-open") {
        const id = readRoute().conversation;
        if (id && id !== "new") void store.open(id);
    } else if (action === "new") {
        store.startNew({ minhag: window.appState?.prefs?.community });
    }
    scheduleRender();
}

function onAuthChanged(event) {
    const signedIn = Boolean(event?.detail?.signedIn) && isSignedIn();
    const was = ui.signedIn;
    ui.signedIn = signedIn;
    if (!ui.authResolved) {
        ui.authResolved = true;
        resolveAuth();
    }
    syncSignInHint();
    if (signedIn && !was) {
        ui.listLoadedAt = 0;
        maybeLoadList(true);
        if (ui.awaitingOpenId) {
            const id = ui.awaitingOpenId;
            ui.awaitingOpenId = null;
            void store.open(id);
        }
    } else if (!signedIn && was) {
        // Signed out: never leave someone else's thread on screen.
        store.startNew({ minhag: window.appState?.prefs?.community });
        closePanel();
    }
    scheduleRender();
}

function syncTray() {
    if (!els.tray) return;
    const want = document.body.classList.contains("mobile-search-open") && effectiveLayout() === "mobile";
    const showing = !els.tray.classList.contains("hidden") && !els.tray.classList.contains("is-hiding");
    if (want && !showing) {
        hideTip();
        show(els.tray, "drawer");
    } else if (!want && showing) {
        if (ui.popTrigger === els.trayHistory) closeHistoryPop();
        hide(els.tray, "drawer");
    }
}

function relocalize() {
    document.querySelectorAll("[data-aria-en]").forEach((el) => {
        const label = lang() === "he" ? el.dataset.ariaHe : el.dataset.ariaEn;
        if (!label) return;
        el.setAttribute("aria-label", label);
        if (el.hasAttribute("title")) el.title = label;
    });
    ui.lastListRef = null;
    els.minhagSelect.dataset.key = "";
    els.sourcesList.dataset.key = "";
    applyAttributes();
    if (ui.tipAnchor) requestAnimationFrame(positionTip);
    scheduleRender();
}

function bindEvents() {
    document.addEventListener("click", onDocumentClick);
    document.addEventListener("keydown", onKeydown);
    document.addEventListener("shelah:auth-changed", onAuthChanged);

    els.scrim.addEventListener("click", () => closePanel());
    els.menuBtn.addEventListener("click", () => openMenu(els.menuBtn));
    els.chip.addEventListener("click", () => openMenu(els.chip));
    els.pipOpen.addEventListener("click", () => setSize("overlay"));
    els.jump.addEventListener("click", () => scrollToLatest(true));
    els.scroller.addEventListener("scroll", updateJumpButton, { passive: true });
    els.noticeAction.addEventListener("click", onNoticeAction);
    els.minhagSelect.addEventListener("change", () => {
        store.setDraftMinhag(els.minhagSelect.value);
    });
    els.themeBtn?.addEventListener("click", () => {
        const dark = document.documentElement.getAttribute("data-theme") === "dark";
        if (typeof window.setThemePreference === "function") window.setThemePreference(dark ? "light" : "dark");
    });

    els.composer.addEventListener("submit", (event) => {
        event.preventDefault();
        void submitComposer();
    });
    els.input.addEventListener("input", autosize);
    els.input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
            event.preventDefault();
            void submitComposer();
        }
    });

    els.topAsk?.addEventListener("click", onTopAsk);
    els.phoneAsk?.addEventListener("click", onPhoneAsk);
    els.tipTry?.addEventListener("click", tryFromTip);
    els.tipDismiss?.addEventListener("click", dismissTip);
    els.tip?.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            event.stopPropagation();
            dismissTip();
        }
    });
    // The drawer's text previews load when opened (toggle doesn't bubble).
    els.sourcesList.addEventListener("toggle", (event) => {
        if (event.target instanceof HTMLDetailsElement) void loadPreview(event.target);
    }, true);
    els.sources.addEventListener("toggle", () => {
        if (els.sources.open) animateSourceCards();
    });

    els.trayAsk?.addEventListener("click", () => {
        const input = document.getElementById("searchInput");
        const question = input?.value.trim() || "";
        window.hideSearchSuggestions?.();
        window.setMobileSearchExpanded?.(false);
        if (input && question) input.value = "";
        void openConversation({ question, trigger: els.trayAsk });
    });

    const mq = window.matchMedia(MOBILE_QUERY);
    const onLayout = () => {
        const next = effectiveLayout();
        syncTray();
        if (ui.tipAnchor) {
            // The tip's anchor differs per layout: re-seat it.
            hideTip();
            maybeShowTip();
        }
        if (next === ui.layout) return;
        const previous = ui.layout;
        ui.layout = next;
        closeMenu();
        closeHistoryPop();
        if (!ui.open) {
            applyAttributes();
            return;
        }
        // Crossing the breakpoint: re-seat the panel for the new layout.
        if (previous === "mobile" && ui.size === "mini") hide(els.pip, "fade");
        els.panel.classList.add("hidden");
        applyVisibility({ opening: true });
        scheduleRender();
    };
    mq.addEventListener?.("change", onLayout);

    new MutationObserver(syncTray).observe(document.body, { attributes: true, attributeFilter: ["class"] });
    new MutationObserver(relocalize).observe(document.documentElement, { attributes: true, attributeFilter: ["lang", "dir"] });
    window.addEventListener("resize", () => {
        if (!els.pop.classList.contains("hidden") && ui.popTrigger) positionPop(ui.popTrigger);
        if (ui.tipAnchor) positionTip();
    }, { passive: true });

    watchTipBlockingMenus();
    installMiniDrag();
    installSheetSwipe();
}

// ── install ────────────────────────────────────────────────────────────

export function installConversationUI({ storeFactory = createConversationStore } = {}) {
    if (ui.installed) return window.ShelahConversationUI;
    els = collectElements();
    if (!els) return null;
    ui.installed = true;

    try {
        iconPaths = JSON.parse(document.getElementById("convIconPaths")?.textContent || "{}");
    } catch (_err) {
        iconPaths = {};
    }

    store = storeFactory({
        getPrefs: () => ({
            community: window.appState?.prefs?.community,
            mode: currentMode(),
            language: lang(),
        }),
    });
    store.subscribe(scheduleRender);

    ui.layout = effectiveLayout();
    ui.signedIn = isSignedIn();
    // No Clerk on this deployment (e.g. local dev): nothing to wait for.
    if (!clerkConfigured() || ui.signedIn) {
        ui.authResolved = true;
        resolveAuth();
    }

    bindEvents();
    relocalize();
    syncSignInHint();
    syncTray();
    if (ui.signedIn) maybeLoadList(true);
    maybeShowTip();

    const api = {
        hydrate,
        open: openConversation,
        close: () => closePanel(),
        setSize,
        openHistory: (trigger) => openHistoryPop(trigger),
        getStore: () => store,
        // The single-answer modal counts as using the AI too (handleAiSearch).
        markAiUsed,
    };
    window.ShelahConversationUI = api;
    return api;
}
