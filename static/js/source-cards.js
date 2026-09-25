// Source cards shared by both AI answer surfaces: the single-answer modal
// (#aiAssistantModal, populateAiModal() in templates/index.html) and the
// conversation panel (static/js/conversation-ui.js). Pure string builders --
// no DOM, no fetch -- so both surfaces render a cited source identically and
// tests_js/source_cards.test.js can exercise them directly. Styles are the
// global .ai-source-box / .ai-src-* / .src-ext-link rules in static/css/ai.css.
//
// main.js exposes this module as window.ShelahSourceCards for the classic
// inline script.

const TANAKH = /^(Genesis|Exodus|Leviticus|Numbers|Deuteronomy|Joshua|Judges|I?\s*Samuel|I?\s*Kings|Isaiah|Jeremiah|Ezekiel|Hosea|Joel|Amos|Obadiah|Jonah|Micah|Nahum|Habakkuk|Zephaniah|Haggai|Zechariah|Malachi|Psalms|Proverbs|Job|Song of Songs|Ruth|Lamentations|Ecclesiastes|Esther|Daniel|Ezra|Nehemiah|I?\s*Chronicles)\b/i;
const TALMUD = /^(Berachot|Shabbat|Eruvin|Pesachim|Shekalim|Yoma|Sukkah|Beitzah|Rosh Hashanah|Taanit|Megillah|Moed Katan|Chagigah|Yevamot|Ketubot|Nedarim|Nazir|Sotah|Gittin|Kiddushin|Bava Kamma|Bava Metzia|Bava Batra|Sanhedrin|Makkot|Shevuot|Avodah Zarah|Horayot|Zevachim|Menachot|Chullin|Bechorot|Arachin|Temurah|Keritot|Niddah|Talmud\b)\b/i;
const HALACHA = /^(Shulchan Aruch|Mishnah Berurah|Kitzur Shulchan Aruch|Aruch HaShulchan|Ben Ish Hai|Mishneh Torah|Rambam|Tur\b|Sefer HaMitzvot|Sefer HaChinuch)\b/i;
const RESPONSA = /^(Igrot Moshe|Yabia Omer|Tzitz Eliezer|Chazon Ish|Minchat Yitzchak|Chelkat Yaakov|Piskei Teshuvot|Noda BiYehudah|Chatam Sofer|Responsa|Maharsha|Maharam|She'elot)\b/i;
const MISHNAH = /^(Mishnah\b|Pirkei Avot|Avot)\b/i;
const ON_SEFARIA = /^(Shulchan Aruch|Mishneh Torah|Tur\b|Kitzur Shulchan Aruch|Ben Ish Hai|Aruch HaShulchan|Mishnah Berurah|Mishnah\b|Talmud|Sefer HaMitzvot|Sefer HaChinuch|Rambam|Pirkei Avot|Avot|Siddur)\b/i;
const PRACTICAL_HALACHA = /^(Shulchan Aruch|Mishnah Berurah|Kitzur Shulchan Aruch|Aruch HaShulchan|Ben Ish Hai|Piskei Teshuvot|Igrot Moshe|Chazon Ish|Tzitz Eliezer|Yabia Omer)\b/i;
const DAF = /^([A-Za-z\s]+?)\s+(\d+[ab])/;

const KIND_LABELS = {
    tanakh: { en: "Tanakh", he: "תנ״ך" },
    talmud: { en: "Talmud", he: "תלמוד" },
    halacha: { en: "Halacha", he: "הלכה" },
    responsa: { en: "Responsa", he: "שו״ת" },
    mishnah: { en: "Mishnah", he: "משנה" },
    other: { en: "Source", he: "מקור" },
};

export function escapeHtml(value) {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

// Transliteration varies by style guide ("Aruch"/"Arukh", "Tanach"/"Tanakh"),
// and the AI and Sefaria don't always agree, so classification runs against
// this normalized form -- never against the original, which is kept for
// display and for building the actual URLs.
export function normalizeRefForMatch(ref) {
    return String(ref || "").trim().replace(/kh/gi, "ch");
}

export function sourceKind(ref) {
    const r = normalizeRefForMatch(ref);
    if (TANAKH.test(r)) return "tanakh";
    if (TALMUD.test(r)) return "talmud";
    if (HALACHA.test(r)) return "halacha";
    if (RESPONSA.test(r)) return "responsa";
    if (MISHNAH.test(r)) return "mishnah";
    return "other";
}

export function sourceBadgeHtml(ref, lang = "en") {
    const kind = sourceKind(ref);
    const label = KIND_LABELS[kind][lang === "he" ? "he" : "en"];
    return `<span class="ai-src-badge ai-src-badge--${kind}">${escapeHtml(label)}</span>`;
}

// Every external place a ref can be read, as data:
//   Talmud   -> Dicta + AlHaTorah + Sefaria
//   Tanakh   -> AlHaTorah + Sefaria
//   Sefaria-corpus halachic works -> Sefaria (+ Halachipedia for practical codes)
//   Responsa / anything else      -> HebrewBooks (via a site search, which
//                                     always resolves; its own search is JS-only)
export function externalLinks(ref) {
    const r = String(ref || "").trim();
    if (!r) return [];
    const m = normalizeRefForMatch(r);
    const links = [];
    const daf = m.match(DAF);
    const isTanakh = TANAKH.test(m);
    const isOnSefaria = ON_SEFARIA.test(m);

    if (daf) {
        links.push({ label: "Dicta", href: `https://dicta.org.il/talmudsearch?search=${encodeURIComponent(`${daf[1].trim()} ${daf[2]}`)}` });
        links.push({ label: "AlHaTorah", href: `https://alhatorah.org/Full/${encodeURIComponent(r)}` });
    } else if (isTanakh) {
        links.push({ label: "AlHaTorah", href: `https://alhatorah.org/Full/${encodeURIComponent(r)}` });
    } else if (!isOnSefaria) {
        links.push({ label: "HebrewBooks", href: `https://www.google.com/search?q=${encodeURIComponent(`site:hebrewbooks.org ${r.split(",")[0].trim()}`)}` });
    }
    if (isOnSefaria || daf || isTanakh) {
        links.push({ label: "Sefaria", href: `https://www.sefaria.org/${encodeURIComponent(r)}` });
    }
    if (PRACTICAL_HALACHA.test(m)) {
        const query = r.replace(/[\s,.:;]+\d[\d\s,.:;]*$/, "").trim() || r;
        links.push({ label: "Halachipedia", href: `https://halachipedia.com/index.php?search=${encodeURIComponent(query)}` });
    }
    return links;
}

export function externalLinksHtml(ref) {
    return externalLinks(ref)
        .map((l) => `<a href="${escapeHtml(l.href)}" target="_blank" rel="noopener noreferrer" class="src-ext-link">${escapeHtml(l.label)} ↗</a>`)
        .join("");
}

const stripTags = (value) => String(value || "").replace(/<[^>]*>/gm, "").trim();

// First three lines of a /api/text payload as a bilingual preview body.
export function previewHtml(lines, { max = 3 } = {}) {
    if (!Array.isArray(lines) || !lines.length) return "";
    const head = lines.slice(0, max);
    const he = head.map((l) => stripTags(l?.he)).filter(Boolean);
    const en = head.map((l) => stripTags(typeof l === "string" ? l : l?.en)).filter(Boolean);
    if (!he.length && !en.length) return "";
    let html = '<div class="ai-src-box-body">';
    if (he.length) html += `<div class="ai-src-box-he" dir="rtl">${he.map((t) => `<p>${escapeHtml(t)}</p>`).join("")}</div>`;
    if (en.length) html += `<div class="ai-src-box-en">${en.map((t) => `<p>${escapeHtml(t)}</p>`).join("")}</div>`;
    return `${html}</div>`;
}
