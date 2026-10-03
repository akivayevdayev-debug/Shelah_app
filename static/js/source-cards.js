// Source cards for the conversation panel (static/js/conversation-ui.js),
// which shows both search-bar answers and conversation turns. Pure string
// builders -- no DOM, no fetch -- so every turn renders a cited source
// identically and tests_js/source_cards.test.js can exercise them directly. Styles are the
// global .ai-source-box / .ai-src-* / .src-ext-link rules in static/css/ai.css.
//
// main.js exposes this module as window.ShelahSourceCards for the classic
// inline script.

import { icon } from "./icons.js";

const TANAKH = /^(Genesis|Exodus|Leviticus|Numbers|Deuteronomy|Joshua|Judges|(?:[12]|I{1,2})?\s*Samuel|(?:[12]|I{1,2})?\s*Kings|Isaiah|Jeremiah|Ezekiel|Hosea|Joel|Amos|Obadiah|Jonah|Micah|Nahum|Habakkuk|Zephaniah|Haggai|Zechariah|Malachi|Psalms|Proverbs|Job|Song of Songs|Ruth|Lamentations|Ecclesiastes|Esther|Daniel|Ezra|Nehemiah|(?:[12]|I{1,2})?\s*Chronicles)\b/i;
const TALMUD = /^(Berachot|Shabbat|Eruvin|Pesachim|Shekalim|Yoma|Sukkah|Beitzah|Rosh Hashanah|Taanit|Megillah|Moed Katan|Chagigah|Yevamot|Ketubot|Nedarim|Nazir|Sotah|Gittin|Kiddushin|Bava Kamma|Bava Metzia|Bava Batra|Sanhedrin|Makkot|Shevuot|Avodah Zarah|Horayot|Zevachim|Menachot|Chullin|Bechorot|Arachin|Temurah|Keritot|Niddah|Talmud\b)\b/i;
const HALACHA = /^(Shulchan Aruch|Mishnah Berurah|Kitzur Shulchan Aruch|Aruch HaShulchan|Ben Ish Hai|Mishneh Torah|Rambam|Tur\b|Sefer HaMitzvot|Sefer HaChinuch)\b/i;
const RESPONSA = /^(Igrot Moshe|Yabia Omer|Tzitz Eliezer|Chazon Ish|Minchat Yitzchak|Chelkat Yaakov|Piskei Teshuvot|Noda BiYehudah|Chatam Sofer|Responsa|Maharsha|Maharam|She'elot)\b/i;
const MISHNAH = /^(Mishnah\b|Pirkei Avot|Avot)\b/i;
const ON_SEFARIA = /^(Shulchan Aruch|Mishneh Torah|Tur\b|Kitzur Shulchan Aruch|Ben Ish Hai|Aruch HaShulchan|Mishnah Berurah|Mishnah\b|Talmud|Jerusalem Talmud|Yerushalmi|Sefer HaMitzvot|Sefer HaChinuch|Rambam|Pirkei Avot|Avot|Siddur)\b/i;
// Word-then-optional-more-words, so group 1 can never itself swallow the
// whitespace that has to separate it from the page number -- the old
// `([A-Za-z\s]+?)\s+` let both sides claim the same run of spaces,
// which is SonarCloud javascript:S8786's super-linear-regex shape.
const DAF = /^([A-Za-z]+(?:\s+[A-Za-z]+)*)\s+(\d+[ab])/;
const YERUSHALMI = /\b(?:Yerushalmi|Jerusalem|Yer\.)/i;

// Hebrew names for the sites a source can be read on: AlHaTorah is a brand
// with a Hebrew-script spelling of its own, HebrewBooks is transliterated the
// way Hebrew-language pages write it.
const SITE_LABELS_HE = {
    AlHaTorah: "על התורה",
    HebrewBooks: "היברובוקס",
};

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
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
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
    if (TALMUD.test(r) || isTractateDaf(ref)) return "talmud";
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

// A chapter or verse ref -- "Genesis 1:1", "Exodus 20:8-11", "Psalms 23" --
// as book, chapter and the optional verse range, on the ref's original
// spelling (the kh/ch normalisation above is for classifying, not for URLs).
const TANAKH_REF = /^(.+?)\s+(\d+)(?::(\d+)(?:\s*[-\u2013]\s*(\d+))?)?\s*$/;

// AlHaTorah's pages, which show a passage with its commentaries beside it.
// The site split by corpus: Tanakh lives on mg.alhatorah.org, the Talmud on
// shas.alhatorah.org (the old alhatorah.org/Full/... addresses are 404s now).
// Each takes the book name Sefaria uses, with underscores for spaces and
// Roman numerals for numbered books ("I_Samuel"), then chapter.verse or the
// daf; the site redirects that to its own spelling ("Shemuel_I"). A name it
// does not know lands on a "page does not exist" screen, so names are put in
// Sefaria's spelling first.

// Tractate names as Sefaria spells them, with the spellings people write
// instead. Matching ignores case, spaces, punctuation, "kh"/"ch" and doubled
// letters, so "Berachot", "Brachos" and "Berakhot" are one name.
const TRACTATE_NAMES = {
    Berakhot: ["Berachot", "Brachos", "Berachos", "Berakhos"],
    Shabbat: ["Shabbos", "Shabbath", "Shabat"],
    Eruvin: ["Eiruvin", "Erubin"],
    Pesachim: ["Pesahim"],
    Shekalim: [],
    Yoma: [],
    Sukkah: ["Succah", "Sukka"],
    Beitzah: ["Beitza", "Betzah", "Beza"],
    "Rosh Hashanah": ["Rosh Hashana"],
    Taanit: ["Taanis"],
    Megillah: ["Megilla"],
    "Moed Katan": ["Moed Katon"],
    Chagigah: ["Chagiga", "Hagigah"],
    Yevamot: ["Yevamos", "Yebamot"],
    Ketubot: ["Kesubos", "Ketuvot", "Kesuvos", "Ketubos"],
    Nedarim: [],
    Nazir: ["Nozir"],
    Sotah: ["Sota"],
    Gittin: ["Gitin"],
    Kiddushin: ["Kidushin"],
    "Bava Kamma": ["Bava Kama", "Baba Kamma", "Baba Kama"],
    "Bava Metzia": ["Baba Metzia", "Bava Metsia", "Baba Metsia"],
    "Bava Batra": ["Baba Batra", "Bava Basra", "Baba Basra"],
    Sanhedrin: [],
    Makkot: ["Makos", "Makot", "Makkos"],
    Shevuot: ["Shevuos", "Shavuot", "Shavuos"],
    "Avodah Zarah": ["Avoda Zara", "Avodah Zara"],
    Horayot: ["Horayos"],
    Zevachim: ["Zevahim"],
    Menachot: ["Menachos", "Menahot"],
    Chullin: ["Chulin", "Hullin"],
    Bekhorot: ["Bechorot", "Bechoros", "Bekhoros"],
    Arakhin: ["Arachin", "Erchin"],
    Temurah: ["Temura"],
    Keritot: ["Kerisos", "Kereitot", "Keritos"],
    Meilah: ["Meila"],
    Tamid: [],
    Niddah: ["Nidda", "Nidah"],
};

function foldName(name) {
    return String(name || "")
        .toLowerCase()
        .replace(/kh/g, "ch")
        .replace(/[^a-z]/g, "")
        .replace(/os$/, "ot")
        .replace(/(.)\1+/g, "$1");
}

const TRACTATE_BY_FOLD = new Map();
for (const [canonical, variants] of Object.entries(TRACTATE_NAMES)) {
    for (const spelling of [canonical, ...variants]) TRACTATE_BY_FOLD.set(foldName(spelling), canonical);
}

function alhatorahName(name) {
    return encodeURIComponent(String(name).trim().replace(/\s+/g, "_"));
}

// "Talmud Bavli Berakhot" -> "Berakhot"; names it does not know stay as written.
export function alhatorahTractate(name) {
    const bare = String(name || "").replace(/^(?:Babylonian\s+Talmud|Talmud\s+Bavli|Talmud|Bavli)\s+/i, "").trim();
    return TRACTATE_BY_FOLD.get(foldName(bare)) || bare;
}

// A daf of a tractate under any spelling ("Brachos 2a"), which the TALMUD
// pattern above, written for the standard spellings, does not catch.
function isTractateDaf(ref) {
    const daf = String(ref || "").trim().match(DAF);
    return Boolean(daf) && TRACTATE_BY_FOLD.has(foldName(alhatorahTractate(daf[1])));
}

// "1 Samuel" / "2 Kings" -> "I Samuel" / "II Kings", the form AlHaTorah reads.
const ROMAN = { 1: "I", 2: "II" };
export function alhatorahBook(name) {
    return String(name || "").trim().replace(/^([12])\s+(?=[A-Za-z])/, (_, n) => `${ROMAN[n]} `);
}

// A cited ref in the spelling the reader (Sefaria) resolves: a daf of a
// tractate written "Brachos 2a" or "Talmud Bavli Berakhot 2a" becomes
// "Berakhot 2a". Anything else is returned as written.
export function readerRef(ref) {
    const r = String(ref || "").trim();
    const daf = r.match(DAF);
    if (!daf || YERUSHALMI.test(r)) return r;
    const tractate = alhatorahTractate(daf[1]);
    return TRACTATE_BY_FOLD.has(foldName(tractate)) ? `${tractate}${r.slice(daf[1].length)}` : r;
}

function alhatorahLink(r, daf) {
    if (daf) {
        if (YERUSHALMI.test(r)) return null; // Shas HaMeforash is the Bavli
        return { label: "AlHaTorah", href: `https://shas.alhatorah.org/Full/${alhatorahName(alhatorahTractate(daf[1]))}/${daf[2]}` };
    }
    const parts = r.match(TANAKH_REF);
    if (!parts) return null;
    const verse = parts[3] ? `.${parts[3]}${parts[4] ? `-${parts[4]}` : ""}` : "";
    return { label: "AlHaTorah", href: `https://mg.alhatorah.org/Full/${alhatorahName(alhatorahBook(parts[1]))}/${parts[2]}${verse}` };
}

// Every external place a ref can be read, as data. The text itself is always
// in the reader (a cited ref opens it there), so only what the reader cannot
// give is linked out:
//   Talmud -> AlHaTorah (the daf with its commentaries)
//   Tanakh -> AlHaTorah (the verse with its commentaries)
//   Responsa and other works Sefaria does not carry -> HebrewBooks, through a
//     site search (HebrewBooks has no address for a passage, and its own search
//     only runs in script)
//   Works Sefaria carries (Shulchan Aruch, Mishneh Torah, ...) -> nothing:
//     the reader already is that text.
// Each link carries `label` (the site's English name) and `labelHe`.
export function externalLinks(ref) {
    const r = String(ref || "").trim();
    if (!r) return [];
    const kind = sourceKind(r);
    let link = null;
    if (kind === "talmud") {
        link = alhatorahLink(r, r.match(DAF));
    } else if (kind === "tanakh") {
        link = alhatorahLink(r, null);
    } else if (kind === "responsa" || (kind === "other" && !ON_SEFARIA.test(normalizeRefForMatch(r)))) {
        link = { label: "HebrewBooks", href: `https://www.google.com/search?q=${encodeURIComponent(`site:hebrewbooks.org ${r.split(",")[0].trim()}`)}` };
    }
    return link ? [{ ...link, labelHe: SITE_LABELS_HE[link.label] || link.label }] : [];
}

export function externalLinksHtml(ref, lang = "en") {
    return externalLinks(ref)
        .map((l) => `<a href="${escapeHtml(l.href)}" target="_blank" rel="noopener noreferrer" class="src-ext-link">${escapeHtml(lang === "he" ? l.labelHe : l.label)}${icon("arrow-up-right", { size: 12, className: "src-ext-icon" })}</a>`)
        .join("");
}

// The Hebrew spelling of a cited ref ("ברכות ב׳ א"), as Sefaria writes it in
// a /api/text payload; "" when the payload has none, so callers keep the
// English ref rather than show a bare book title for a specific passage.
export function hebrewRefName(payload) {
    return typeof payload?.heRef === "string" ? payload.heRef.trim() : "";
}

// Non-regex tag strip (SonarCloud javascript:S8786 flagged `/<[^>]*>/gm`,
// even though a negated class is provably linear) -- only drops a `<...>`
// span when it finds the closing `>`, matching the old regex's behavior on
// an unterminated `<` at the end of a truncated string.
function stripTags(value) {
    const text = String(value || "");
    let out = "";
    let from = 0;
    for (let start = text.indexOf("<", from); start !== -1; start = text.indexOf("<", from)) {
        const end = text.indexOf(">", start);
        if (end === -1) break;
        out += text.slice(from, start);
        from = end + 1;
    }
    return (out + text.slice(from)).trim();
}

// Sefaria nests a footnote in the text as <sup class="footnote-marker">1</sup>
// followed by <i class="footnote">...</i> (which may hold <i> of its own). In
// a preview that note would run into the sentence as plain text, so both are
// dropped. A scan, not a regex: it follows the nesting of the <i> tags.
const FOOTNOTE_OPEN = '<i class="footnote"';
const FOOTNOTE_MARKER_OPEN = '<sup class="footnote-marker"';

function dropBlock(text, openTag, closeAt) {
    let out = text;
    for (let start = out.indexOf(openTag); start !== -1; start = out.indexOf(openTag)) {
        const end = closeAt(out, start);
        out = end === -1 ? out.slice(0, start) : out.slice(0, start) + out.slice(end);
    }
    return out;
}

// Where the <i> opened at `start` closes, counting nested <i> tags.
function closeOfItalic(text, start) {
    let depth = 0;
    for (let lt = text.indexOf("<", start); lt !== -1; lt = text.indexOf("<", lt + 1)) {
        const gt = text.indexOf(">", lt);
        if (gt === -1) return -1;
        const tag = text.slice(lt + 1, gt);
        if (tag === "/i") depth -= 1;
        else if (tag === "i" || tag.startsWith("i ")) depth += 1;
        if (depth === 0) return gt + 1;
    }
    return -1;
}

function dropFootnotes(html) {
    const text = String(html || "");
    if (!text.includes("footnote")) return text;
    const withoutMarkers = dropBlock(text, FOOTNOTE_MARKER_OPEN, (t, start) => {
        const close = t.indexOf("</sup>", start);
        return close === -1 ? -1 : close + "</sup>".length;
    });
    return dropBlock(withoutMarkers, FOOTNOTE_OPEN, closeOfItalic);
}

// Sefaria's text carries HTML entities ("&nbsp;") and the Masoretic section
// markers "{פ}" / "{ס}"; shown as text they are noise, so they are decoded
// or dropped before the preview is escaped.
const ENTITIES = { nbsp: " ", amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", thinsp: " ", ensp: " ", emsp: " " };
const SECTION_MARK = /\{[\u05D0-\u05EA]\}/g;

function cleanText(value) {
    return stripTags(dropFootnotes(value))
        .replace(/&(?:#(\d{1,6})|#x([\da-f]{1,5})|([a-z]{2,6}));/gi, (whole, dec, hex, name) => {
            if (name) return ENTITIES[name.toLowerCase()] ?? whole;
            const code = dec ? Number(dec) : Number.parseInt(hex, 16);
            return code > 0 && code < 0x110000 ? String.fromCodePoint(code) : whole;
        })
        .replace(SECTION_MARK, "")
        .replace(/\s+/g, " ")
        .trim();
}

// First three lines of a /api/text payload as a preview body: Hebrew over
// English, or Hebrew alone when the interface is Hebrew. A passage with no
// Hebrew text still shows its English rather than an empty preview.
export function previewHtml(lines, { max = 3, lang = "en" } = {}) {
    if (!Array.isArray(lines) || !lines.length) return "";
    const head = lines.slice(0, max);
    const he = head.map((l) => cleanText(l?.he)).filter(Boolean);
    const en = head.map((l) => cleanText(typeof l === "string" ? l : l?.en)).filter(Boolean);
    if (!he.length && !en.length) return "";
    const showEn = en.length && !(lang === "he" && he.length);
    let html = '<div class="ai-src-box-body">';
    if (he.length) html += `<div class="ai-src-box-he" dir="rtl">${he.map((t) => `<p>${escapeHtml(t)}</p>`).join("")}</div>`;
    if (showEn) html += `<div class="ai-src-box-en">${en.map((t) => `<p>${escapeHtml(t)}</p>`).join("")}</div>`;
    return `${html}</div>`;
}
