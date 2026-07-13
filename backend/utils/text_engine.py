"""
Typography, normalization, and answer-formatting layer for Sh'elah.

Contract: no Flask context. Every symbol here is pure in -> pure out — no
`g`/`session`/`request`/`current_app`, no I/O, no imports from `app`. This
module renders/normalizes AI-answer text for the UI; it does not fetch or
retrieve anything (that's backend/utils/search_provider.py, Phase 2).

Moved verbatim from app.py in Phase 1 of the backend refactor (see plan.md).
"""

import re

HEBREW_DIACRITICS_RE = re.compile(r"[\u0591-\u05C7]")

CLOCK_TIME_LATEX_RE = re.compile(
    r"\$(\d{1,2}:\d{2})\s*\\{1,2}text\{\s*(AM|PM)\s*\}\$",
    re.IGNORECASE,
)
DEBUG_OUTPUT_LINE_PATTERNS = [
    re.compile(r"^\s*#{0,6}\s*conflict\s*flag[s]?\b.*$", re.IGNORECASE),
    re.compile(r"^\s*[-*]\s*conflict\s*flag[s]?\b.*$", re.IGNORECASE),
    re.compile(
        r"^\s*(?:[-*]\s*)?source\s*:\s*community\s*knowledge\b.*$", re.IGNORECASE),
    re.compile(
        r"^\s*(?:[-*]\s*)?no\s+primary\s+sefaria\s+snippet\b.*$", re.IGNORECASE),
]
SECTION_KEY_VALUE_RE = re.compile(
    r"^\s*(?P<key>[A-Za-z][A-Za-z0-9 /()'&-]{2,40}):\s*(?P<value>.+)?$"
)
BOLD_HEADER_RE = re.compile(
    r"^\s*\*\*(?P<title>[A-Za-z][A-Za-z0-9 /()'&-]{2,40})\*\*:?\s*(?P<rest>.*)$"
)
HALAKHIC_VERDICT_RE = re.compile(
    r"\b(prohibited|forbidden|permitted|required|obligatory|invalid|valid|asur|assur|mutar)\b",
    re.IGNORECASE,
)
DOMAIN_REFUSAL_MESSAGE_RE = re.compile(
    r"^Sh'elah is a specialized tool for Halakhic and communal knowledge\. "
    r"I cannot assist with .+?, as it falls outside my specialized domain\."
    r"(?:\s+Please consult with your local Rabbi for a final ruling\.)?$",
    re.DOTALL,
)
UI_SECTION_KEYS = {
    "ruling",
    "reason",
    "conditions",
    "exceptions",
    "practical steps",
    "practical guidance",
    "sources",
    "summary",
}
HALAKHIC_VERDICT_LABELS = {
    "prohibited": "Prohibited",
    "forbidden": "Forbidden",
    "permitted": "Permitted",
    "required": "Required",
    "obligatory": "Obligatory",
    "invalid": "Invalid",
    "valid": "Valid",
    "asur": "Asur",
    "assur": "Assur",
    "mutar": "Mutar",
}

WEB_LAST_RESORT_WARNING = "⚠️ **WARNING:** No matches found in Sefaria or verified customs. The following info is from the general web and may not be Halakhically accurate. Consult a Rabbi."
WEB_LAST_RESORT_WARNING_PLAIN = WEB_LAST_RESORT_WARNING.replace("**", "")
RABBI_FINAL_RULING_FOOTER = "Please consult with your local Rabbi for a final ruling."


def _strip_model_web_warning_prefix(answer_text):
    text = str(answer_text or "").strip()
    if not text:
        return ""

    for marker in (WEB_LAST_RESORT_WARNING, WEB_LAST_RESORT_WARNING_PLAIN):
        if text.startswith(marker):
            text = text[len(marker):].lstrip()

    # Guard against model-inserted markdown separators glued to the warning line.
    text = re.sub(r"^-{3,}\s*", "", text)
    return text.strip()


def _strip_source_attribution_prefix(answer_text):
    text = str(answer_text or "").strip()
    if not text:
        return ""

    lower_text = text.lower()
    if not lower_text.startswith("note: this information was"):
        return text

    # Preferred shape is: note line, blank line, then body.
    if "\n\n" in text:
        return text.split("\n\n", 1)[1].lstrip()

    # If model emitted note + footer in one line, remove up to the footer and keep remainder.
    footer_idx = text.find(RABBI_FINAL_RULING_FOOTER)
    if footer_idx != -1:
        remainder = text[footer_idx + len(RABBI_FINAL_RULING_FOOTER):]
        return remainder.lstrip(" \n\t:-")

    # Otherwise only strip the first line and keep any remaining lines.
    first_newline_idx = text.find("\n")
    if first_newline_idx != -1:
        return text[first_newline_idx + 1:].lstrip()

    return text


def _normalize_ai_answer(answer_text, include_web_warning=False, source_attribution_note="", allow_empty_fallback=True):
    body = _strip_model_web_warning_prefix(answer_text)
    body = _strip_source_attribution_prefix(body)
    body = CLOCK_TIME_LATEX_RE.sub(
        lambda m: f"{m.group(1)} {m.group(2).upper()}", body)

    # Preserve the domain guardrail refusal message exactly as emitted.
    if DOMAIN_REFUSAL_MESSAGE_RE.fullmatch(body):
        if RABBI_FINAL_RULING_FOOTER not in body:
            return f"{body}\n\n{RABBI_FINAL_RULING_FOOTER}"
        return body

    if not body:
        if allow_empty_fallback:
            body = "No verified source found"
        else:
            return ""

    prefix_blocks = []
    if include_web_warning:
        prefix_blocks.append(WEB_LAST_RESORT_WARNING)

    attribution = str(source_attribution_note or "").strip()
    if attribution:
        prefix_blocks.append(attribution)
    elif body.lower() != "no verified source found" and RABBI_FINAL_RULING_FOOTER not in body:
        prefix_blocks.append(RABBI_FINAL_RULING_FOOTER)

    if prefix_blocks:
        return "\n\n".join(prefix_blocks + [body])

    return body


def _should_drop_debug_line(line_text):
    return any(pattern.match(line_text) for pattern in DEBUG_OUTPUT_LINE_PATTERNS)


def _bold_halakhic_verdicts(text):
    def _replace(match):
        token = str(match.group(0) or "")
        canonical = HALAKHIC_VERDICT_LABELS.get(token.lower(), token)
        return f"**{canonical}**"

    return HALAKHIC_VERDICT_RE.sub(_replace, text)


def _normalize_answer_line(raw_line):
    line = str(raw_line or "").strip()
    if not line:
        return [""]

    if _should_drop_debug_line(line):
        return []

    line = re.sub(r"^\s*[•*]\s+", "- ", line)
    line = re.sub(r"^\s*[-–]\s+", "- ", line)

    if line.startswith("# "):
        line = f"## {line[2:].strip()}"

    bold_header = BOLD_HEADER_RE.match(line)
    if bold_header:
        title = bold_header.group("title").strip().rstrip(":")
        rest = (bold_header.group("rest") or "").strip()
        output = [f"### {title}"]
        if rest:
            output.append(_bold_halakhic_verdicts(rest))
        return output

    key_value = SECTION_KEY_VALUE_RE.match(line)
    if key_value:
        key = key_value.group("key").strip()
        value = (key_value.group("value") or "").strip()
        if key.lower() in UI_SECTION_KEYS:
            output = [f"### {key}"]
            if value:
                output.append(_bold_halakhic_verdicts(value))
            return output

    return [_bold_halakhic_verdicts(line)]


def _collapse_markdown_spacing(lines):
    normalized = []
    prev_blank = True

    for line in lines:
        text = str(line or "")
        is_blank = not text.strip()

        if is_blank:
            if not prev_blank:
                normalized.append("")
            prev_blank = True
            continue

        if text.startswith("##"):
            if normalized and normalized[-1] != "":
                normalized.append("")
            normalized.append(text)
            prev_blank = False
            continue

        normalized.append(text)
        prev_blank = False

    while normalized and not normalized[0].strip():
        normalized.pop(0)
    while normalized and not normalized[-1].strip():
        normalized.pop()

    return normalized


def _format_ui_answer(answer_text):
    lines = []
    for raw_line in str(answer_text or "").splitlines():
        lines.extend(_normalize_answer_line(raw_line))

    lines = _collapse_markdown_spacing(lines)
    if not lines:
        return ""

    has_headers = any(line.startswith("##") for line in lines)
    if not has_headers and str(answer_text or "").strip().lower() != "no verified source found":
        lines = ["## Ruling", "", lines[0]] + lines[1:]
        lines = _collapse_markdown_spacing(lines)

    return "\n".join(lines).strip()
