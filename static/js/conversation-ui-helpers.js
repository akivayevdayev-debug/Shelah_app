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
        default:
            return {
                tone: "warn",
                text: tr({ en: "Something went wrong. Please try again.", he: "משהו השתבש. נסה שוב." }, lang),
                action: null,
            };
    }
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
