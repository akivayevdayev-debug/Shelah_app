// Pure helpers for static/js/conversation-ui.js -- no DOM, no globals, so
// tests_js/conversation_ui_helpers.test.js can exercise them directly.

import { ERROR_CODES } from "./conversation-api.js";

export const SIZES = Object.freeze(["mini", "overlay", "full"]);
export const DEFAULT_SIZE = "overlay";
export const CORNERS = Object.freeze(["br", "bl", "tr", "tl"]);

// Bilingual pick: tr({ en, he }, lang).
function tr(pair, lang) {
    return lang === "he" ? pair.he : pair.en;
}

export function normalizeSize(size) {
    return SIZES.includes(size) ? size : DEFAULT_SIZE;
}

// The header's single "expand" control grows the panel one step:
// mini -> overlay -> full (full stays full).
export function expandedSize(size) {
    const current = normalizeSize(size);
    if (current === "mini") return "overlay";
    return "full";
}

// Which sizes block the page underneath (scrim / focus trap / scroll lock).
// The mini widget and the phone's minimised bar leave the page usable.
export function isModalSize(size) {
    return normalizeSize(size) !== "mini";
}

// Compact "time ago" for list rows: now / 5m / 3h / Yesterday / date.
export function relativeTime(iso, { now = Date.now(), lang = "en" } = {}) {
    const then = Date.parse(iso || "");
    if (!Number.isFinite(then)) return "";
    const seconds = Math.max(0, Math.round((now - then) / 1000));
    if (seconds < 60) return tr({ en: "now", he: "עכשיו" }, lang);
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return tr({ en: `${minutes}m`, he: `לפני ${minutes} דק׳` }, lang);
    const hours = Math.round(minutes / 60);
    if (hours < 24) return tr({ en: `${hours}h`, he: `לפני ${hours} שע׳` }, lang);
    const days = Math.floor(hours / 24);
    if (days === 1) return tr({ en: "Yesterday", he: "אתמול" }, lang);
    const date = new Date(then);
    try {
        return date.toLocaleDateString(lang === "he" ? "he-IL" : "en-US", {
            month: "short",
            day: "numeric",
            ...(days > 300 ? { year: "numeric" } : {}),
        });
    } catch (_err) {
        return date.toISOString().slice(0, 10);
    }
}

// The heading a saved conversation sits under in the list: pinned ones first,
// then by the calendar day it last moved ("Today", "Yesterday", the 6 days
// before that, then everything older).
export function conversationGroup(item, { now = Date.now() } = {}) {
    if (item?.pinnedAt) return "pinned";
    const then = Date.parse(item?.updatedAt || item?.createdAt || "");
    if (!Number.isFinite(then)) return "earlier";
    const startOfToday = new Date(now);
    startOfToday.setHours(0, 0, 0, 0);
    const behind = startOfToday.getTime() - then;
    const day = 24 * 60 * 60 * 1000;
    if (behind <= 0) return "today";
    if (behind <= day) return "yesterday";
    if (behind <= 6 * day) return "week";
    return "earlier";
}

export function conversationGroupLabel(group, lang = "en") {
    const labels = {
        pinned: { en: "Pinned", he: "מוצמדות" },
        today: { en: "Today", he: "היום" },
        yesterday: { en: "Yesterday", he: "אתמול" },
        week: { en: "Previous 7 days", he: "7 הימים הקודמים" },
        earlier: { en: "Earlier", he: "קודם" },
    };
    return tr(labels[group] || labels.earlier, lang);
}

// Localized community name; the stored value is the English option value.
export function communityName(minhag, lang = "en", options = []) {
    const value = String(minhag || "").trim();
    if (!value || value.toLowerCase() === "all") return tr({ en: "All communities", he: "כל הקהילות" }, lang);
    const match = options.find((o) => o.value === value);
    return (lang === "he" && match?.he) || value;
}

// "Answers in this conversation follow Sefardic practice."
export function lockLine(minhag, lang = "en", options = []) {
    const value = String(minhag || "").trim();
    if (!value || value.toLowerCase() === "all") {
        return tr({
            en: "Answers in this conversation draw on all communities' practice.",
            he: "התשובות בשיחה זו מתבססות על מנהגי כל הקהילות.",
        }, lang);
    }
    const name = communityName(value, lang, options);
    return tr({
        en: `Answers in this conversation follow ${name} practice.`,
        he: `התשובות בשיחה זו לפי מנהג ${name}.`,
    }, lang);
}

function minutesText(seconds, lang) {
    const minutes = Math.max(1, Math.ceil(Number(seconds) / 60));
    return tr({ en: `${minutes} min`, he: `${minutes} דק׳` }, lang);
}

// The inline notice for store.lastError. `action` names what the notice's
// button does: "sign-in" | "retry-open" | "new" | null.
export function noticeFor(error, lang = "en") {
    if (!error) return null;
    const retryAfter = Number(error.retryAfter);
    const hasWait = Number.isFinite(retryAfter) && retryAfter > 0;
    switch (error.code) {
        case ERROR_CODES.BUDGET_EXHAUSTED:
            return {
                tone: "warn",
                text: tr({
                    en: "You've reached today's AI limit. It resets tomorrow.",
                    he: "הגעת למגבלת הבינה המלאכותית היומית. היא מתאפסת מחר.",
                }, lang),
                action: null,
            };
        case ERROR_CODES.AI_PAUSED:
            return {
                tone: "warn",
                text: hasWait
                    ? tr({
                        en: `Sh'elah's AI is paused. Try again in ${minutesText(retryAfter, lang)}.`,
                        he: `הבינה המלאכותית מושהית. נסה שוב בעוד ${minutesText(retryAfter, lang)}.`,
                    }, lang)
                    : tr({ en: "Sh'elah's AI is paused for a moment. Try again soon.", he: "הבינה המלאכותית מושהית לרגע. נסה שוב בקרוב." }, lang),
                action: null,
            };
        case ERROR_CODES.RATE_LIMITED:
            return {
                tone: "warn",
                text: hasWait
                    ? tr({
                        en: `Too many questions at once. Try again in ${Math.ceil(retryAfter)}s.`,
                        he: `יותר מדי שאלות בבת אחת. נסה שוב בעוד ${Math.ceil(retryAfter)} שניות.`,
                    }, lang)
                    : tr({ en: "Too many questions at once. Try again shortly.", he: "יותר מדי שאלות בבת אחת. נסה שוב בעוד רגע." }, lang),
                action: null,
            };
        case ERROR_CODES.UNAUTHORIZED:
            return {
                tone: "warn",
                text: tr({ en: "Your session ended. Sign in again to continue.", he: "ההתחברות הסתיימה. התחבר שוב כדי להמשיך." }, lang),
                action: "sign-in",
            };
        case ERROR_CODES.NOT_FOUND:
            return {
                tone: "warn",
                text: tr({ en: "This conversation no longer exists.", he: "השיחה הזו כבר לא קיימת." }, lang),
                action: "new",
            };
        case ERROR_CODES.NETWORK:
            return {
                tone: "warn",
                text: tr({ en: "Couldn't reach Sh'elah. Check your connection.", he: "לא ניתן להתחבר לש׳אלה. בדוק את החיבור." }, lang),
                action: null,
            };
        case ERROR_CODES.ANSWER_INCOMPLETE:
            // Rendered on the turn itself ("Check again"), not as a banner.
            return null;
        // Search-bar answers (/ask, conversation-store.js askSearch).
        case "turnstile_required":
            return {
                tone: "warn",
                text: tr({
                    en: "Please complete the quick verification check, then try again.",
                    he: "אנא השלם את בדיקת האימות הקצרה ונסה שוב.",
                }, lang),
                action: null,
            };
        case "timeout":
            return {
                tone: "warn",
                text: tr({ en: "The answer took too long. Please try again.", he: "התשובה התעכבה יותר מדי. נסה שוב." }, lang),
                action: null,
            };
        case "guest_cap_reached":
            // The sign-in modal says the rest (guestCapCopy); this is what is
            // left on the page once it is dismissed.
            return {
                tone: "info",
                text: guestCapCopy(error.reason, error.usage, lang).title,
                action: "sign-in",
            };
        default:
            return {
                tone: "warn",
                text: tr({ en: "Something went wrong. Please try again.", he: "משהו השתבש. נסה שוב." }, lang),
                action: null,
            };
    }
}

// ── signed-out limits (backend/guest_cap.py) ──────────────────────────────

// How many questions a guest has left, below which the composer warns.
export const GUEST_WARN_AT = 2;

// The sign-in modal's words when a guest question is refused. `reason` is the
// server's: "thread_questions" | "thread_tokens" | "daily"; `usage` its counters.
export function guestCapCopy(reason, usage, lang = "en") {
    const limit = Number(usage?.questions_limit) > 0 ? Number(usage.questions_limit) : 8;
    if (reason === "daily") {
        return {
            title: tr({ en: "You've used today's guest questions", he: "השתמשתם בשאלות האורחים של היום" }, lang),
            body: tr({
                en: "Sign in or create a free account to keep asking. Your conversations will be saved.",
                he: "התחברו או פתחו חשבון חינם כדי להמשיך לשאול. השיחות שלכם יישמרו.",
            }, lang),
        };
    }
    const title = tr({
        en: "You've reached the guest limit for this conversation",
        he: "הגעתם למגבלת האורחים בשיחה הזו",
    }, lang);
    const waiting = tr({
        en: "This conversation, its sources, and the question you just typed will be waiting for you.",
        he: "השיחה, המקורות והשאלה שהקלדתם יחכו לכם.",
    }, lang);
    const allowance = reason === "thread_tokens"
        ? tr({
            en: "This conversation has used its guest allowance. Sign in or create a free account to keep going.",
            he: "השיחה הזו ניצלה את מכסת האורחים שלה. התחברו או פתחו חשבון חינם כדי להמשיך.",
        }, lang)
        : tr({
            en: `Guests can ask up to ${limit} questions per conversation. Sign in or create a free account to keep going.`,
            he: `אורחים יכולים לשאול עד ${limit} שאלות בשיחה. התחברו או פתחו חשבון חינם כדי להמשיך.`,
        }, lang);
    return { title, body: `${allowance} ${waiting}` };
}

// The line above the composer as a guest's allowance runs low, or null while
// there is plenty (or no count at all, e.g. a stored answer or a signed-in
// thread). `usage` is the `guest` object an answer carries.
export function guestBannerText(usage, lang = "en") {
    if (!usage || !Number.isFinite(Number(usage.remaining))) return null;
    const left = Number(usage.remaining);
    if (left > GUEST_WARN_AT) return null;
    const today = usage.binding === "daily";
    if (left <= 0) {
        return today
            ? tr({ en: "You've used today's guest questions. Sign in to keep asking.", he: "השתמשתם בשאלות האורחים של היום. התחברו כדי להמשיך לשאול." }, lang)
            : tr({ en: "You've reached the guest limit for this conversation. Sign in to keep going.", he: "הגעתם למגבלת האורחים בשיחה הזו. התחברו כדי להמשיך." }, lang);
    }
    if (today) {
        return left === 1
            ? tr({ en: "1 guest question left today. Sign in to keep asking.", he: "נותרה שאלת אורח אחת להיום. התחברו כדי להמשיך לשאול." }, lang)
            : tr({ en: `${left} guest questions left today. Sign in to keep asking.`, he: `נותרו ${left} שאלות אורח להיום. התחברו כדי להמשיך לשאול.` }, lang);
    }
    return left === 1
        ? tr({ en: "1 question left in this guest conversation. Sign in to keep going.", he: "נותרה שאלה אחת בשיחת האורחים הזו. התחברו כדי להמשיך." }, lang)
        : tr({ en: `${left} questions left in this guest conversation. Sign in to keep going.`, he: `נותרו ${left} שאלות בשיחת האורחים הזו. התחברו כדי להמשיך.` }, lang);
}

// Nearest corner for a dragged mini widget, from its centre point.
export function snapCorner({ x, y }, { width, height }) {
    const right = x >= width / 2;
    const bottom = y >= height / 2;
    return `${bottom ? "b" : "t"}${right ? "r" : "l"}`;
}

// Mirror a physical corner for RTL (data-corner uses logical inline sides).
export function logicalCorner(corner, rtl) {
    if (!CORNERS.includes(corner)) return "br";
    if (!rtl) return corner;
    return corner[0] + (corner[1] === "r" ? "l" : "r");
}

// Excerpt in the reader's language, falling back to the other one.
export function citationExcerpt(citation, lang = "en") {
    if (!citation) return "";
    return lang === "he"
        ? citation.excerptHe || citation.excerptEn || ""
        : citation.excerptEn || citation.excerptHe || "";
}

// Change-detection key for a rendered transcript turn.
export function turnSignature(message) {
    if (!message) return "";
    const cites = (message.citations || []).map((c) => c.ref).join("|");
    return `${message.role}:${message.status}:${message.content.length}:${message.content.slice(-24)}:${cites}`;
}
