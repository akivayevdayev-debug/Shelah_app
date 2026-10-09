// Numbered source markers in an AI answer, the way Gemini shows its sources:
// a small numbered chip after the claim a source supports, and the sources
// themselves in one list under the answer (static/js/conversation-ui.js).
//
// The model writes "...kindling is forbidden.[1][2]" -- each number is a
// position in its `sources` list, renumbered server-side so it matches the
// cleaned list (backend/citation_markers.py). This module turns those
// markers in the rendered answer into chips, and drops the "Sources" list the
// server also appends to the answer's markdown, since the sources area under
// the answer already shows it. Pure string functions, no DOM, so
// tests_js/citation_markers.test.js can exercise them directly.

// A marker is "[1]", "[1, 2]" or "[1-3]" -- digits only, so "[2a]" and
// "[the Rema]" stay text -- and a run of adjacent ones is one group.
const ONE = String.raw`\[\d{1,2}(?:\s*[,;]\s*\d{1,2}|\s*[-–]\s*\d{1,2})*\]`;
const RUN = new RegExp(String.raw`([ \t]*)((?:${ONE})+)`, "g");
const NUMBER_OR_RANGE = /(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?/g;

// The conversation UI shows at most this many sources.
export const MAX_MARKER = 10;

export function escapeAttr(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
}

// The source numbers a run of markers names, ascending and without repeats:
// "[3][1]" -> [1, 3], "[1-3]" -> [1, 2, 3].
export function markerNumbers(run) {
    const found = new Set();
    for (const match of String(run || "").matchAll(NUMBER_OR_RANGE)) {
        const start = Number(match[1]);
        let end = match[2] ? Number(match[2]) : start;
        if (end < start || end - start >= MAX_MARKER) end = start;
        for (let n = start; n <= end; n += 1) found.add(n);
    }
    return [...found].sort((a, b) => a - b);
}

// `text` as alternating plain text and marker groups:
// [{ text }, { numbers: [1, 2] }, { text }]. The spaces before a group belong
// to the group (a chip sits against the word it follows).
export function splitMarkers(text) {
    const source = String(text ?? "");
    const parts = [];
    let last = 0;
    for (const match of source.matchAll(RUN)) {
        if (match.index > last) parts.push({ text: source.slice(last, match.index) });
        parts.push({ numbers: markerNumbers(match[2]) });
        last = match.index + match[0].length;
    }
    if (last < source.length) parts.push({ text: source.slice(last) });
    return parts;
}

// A sentence boundary inside a span: terminal punctuation (plus closing quotes
// or brackets), whitespace, then more text. Initials and common abbreviations
// ("R. Yochanan", "Dr. Smith") end in a period but not a sentence.
const BOUNDARY = /[.!?\u2026\u05c3]+["'\u201d\u2019)\]]*\s+(?=\S)/g;
const ABBREVIATIONS = new Set([
    "r", "rabbi", "rav", "dr", "mr", "mrs", "st", "vs", "cf", "etc", "ibid", "e.g", "i.e",
    "b", "bar", "ben", "no", "vol", "ch", "chap", "sec", "par", "ex", "lev", "num", "deut", "gen",
]);
// Markers separated only by spaces ("[1] [2]") belong to one sentence too.
const SPACED_RUN = new RegExp(String.raw`([ \t]*)((?:${ONE})(?:[ \t]*(?:${ONE}))*)`, "g");

function sentenceCount(span) {
    const text = span.trim();
    if (!text) return 0;
    let count = 1;
    for (const match of text.matchAll(BOUNDARY)) {
        const word = /(\S+)$/.exec(text.slice(0, match.index));
        const previous = word ? word[1].replace(/^[(["'\u201c]+|[(["'\u201c]+$/g, "").toLowerCase() : "";
        if (ABBREVIATIONS.has(previous) || /^[a-z]$/.test(previous)) continue;
        count += 1;
    }
    return count;
}

function placeInLine(line) {
    const runs = [...line.matchAll(SPACED_RUN)];
    if (!runs.length) return line;
    const spans = [];
    const cited = [];
    let position = 0;
    for (const match of runs) {
        spans.push(line.slice(position, match.index));
        cited.push(markerNumbers(match[2]));
        position = match.index + match[0].length;
    }
    const tail = line.slice(position);
    let out = "";
    spans.forEach((span, index) => {
        let numbers = cited[index];
        const following = index + 1;
        if (following < spans.length && sentenceCount(spans[following]) === 1) {
            numbers = numbers.filter((n) => !cited[following].includes(n));
        }
        out += (numbers.length ? span.trimEnd() : span) + numbers.map((n) => `[${n}]`).join("");
    });
    return out + tail;
}

// `markdown` with each marker at the end of the excerpt it backs, once per run
// of consecutive sentences on one source -- "Kindling is forbidden.[1] Cooking
// too.[1][2]" becomes "Kindling is forbidden. Cooking too.[1][2]" -- instead of
// a "[1]" after every sentence or all of them gathered at the paragraph's end.
// Mirrors backend/citation_markers.py's place_markers, which the server applies
// to a new answer; applying it here too tidies older stored answers. Idempotent.
// Lines inside a code fence are left alone.
export function placeMarkers(markdown) {
    const lines = String(markdown ?? "").split("\n");
    let inFence = false;
    return lines.map((line) => {
        if (line.trimStart().startsWith("```")) {
            inFence = !inFence;
            return line;
        }
        return inFence ? line : placeInLine(line);
    }).join("\n");
}

// An answer's markdown without its trailing "Sources" block (the label line
// and the bullet list under it, which the server renders last). Left whole when
// anything else follows the list, so a stray "Sources" word mid-answer never
// eats text.
const SOURCES_LABELS = new Set(["sources", "מקורות"]);
const LIST_ITEM = /^(?:[-*•]|\d+[.)])\s/;

export function stripSourcesBlock(markdown) {
    const text = String(markdown ?? "");
    const lines = text.split("\n");
    for (let i = lines.length - 1; i >= 0; i -= 1) {
        const line = lines[i].trim();
        if (!line || LIST_ITEM.test(line)) continue;
        const label = line.replace(/^#+\s*/, "").replace(/[*_:：]/g, "").trim().toLowerCase();
        return SOURCES_LABELS.has(label) ? lines.slice(0, i).join("\n").trimEnd() : text;
    }
    return text;
}

// Chips only go in text, never inside a link, code or a control.
const SKIP_TAGS = new Set(["a", "code", "pre", "button", "script", "style", "textarea"]);
// A comment, or a tag with its quoted attribute values kept whole (an
// attribute may contain ">"): linear, since each branch consumes a distinct
// kind of character.
const TAG = /<!--[\s\S]*?-->|<\/?[A-Za-z][^\s>/]*(?:"[^"]*"|'[^']*'|[^'">])*>/g;
const TAG_NAME = /^<(\/?)([A-Za-z][^\s>/]*)/;

function chipHtml(number, citation, idPrefix, label) {
    const name = label(number, citation);
    return `<button type="button" class="conv-mark" data-mark="${number}" data-mark-target="${escapeAttr(idPrefix)}-${number}"`
        + ` aria-haspopup="dialog" aria-expanded="false" aria-label="${escapeAttr(name)}">${number}</button>`;
}

function markersToChips(text, citations, idPrefix, label) {
    return splitMarkers(text).map((part) => {
        if (part.text !== undefined) return part.text;
        const chips = part.numbers
            .filter((n) => n >= 1 && n <= citations.length && citations[n - 1]?.ref)
            .map((n) => chipHtml(n, citations[n - 1], idPrefix, label));
        // A number with no source behind it renders nothing rather than a dead chip.
        return chips.length ? `<span class="conv-marks">${chips.join("")}</span>` : "";
    }).join("");
}

// `html` (an answer already rendered and sanitised) with every marker group
// in its text replaced by chips. `citations` is the answer's source list
// ([{ ref }], in order); `idPrefix` is what the matching list items' ids
// start with ("<idPrefix>-2" is the second source), so a chip can find its row.
export function injectMarkers(html, { citations = [], idPrefix = "cite", label = (n, c) => `Source ${n}: ${c.ref}` } = {}) {
    const source = String(html ?? "");
    if (!source.includes("[")) return source;
    let out = "";
    let last = 0;
    let skipDepth = 0;
    const convert = (text) => (skipDepth > 0 ? text : markersToChips(text, citations, idPrefix, label));
    for (const match of source.matchAll(TAG)) {
        out += convert(source.slice(last, match.index)) + match[0];
        last = match.index + match[0].length;
        const tag = TAG_NAME.exec(match[0]);
        if (tag && SKIP_TAGS.has(tag[2].toLowerCase()) && !match[0].endsWith("/>")) {
            skipDepth = Math.max(0, skipDepth + (tag[1] ? -1 : 1));
        }
    }
    return out + convert(source.slice(last));
}

// The id of the list item for source `number` of the answer `messageId`:
// ids may only carry letters, digits, "-" and "_", so a message id from
// anywhere is made safe first.
export function citeIdPrefix(messageId) {
    const safe = String(messageId ?? "").replace(/[^A-Za-z0-9_-]/g, "");
    return `conv-cite-${safe || "answer"}`;
}
