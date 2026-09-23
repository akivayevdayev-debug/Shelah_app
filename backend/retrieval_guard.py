"""
Indirect-prompt-injection screen for retrieved third-party text
(docs/AI_SECURITY_REVIEW.md M2).

validate_user_query() runs PROMPT_INJECTION_PATTERNS over the *user's own*
question. Text the app fetches from publicly-editable or crowd-sourced sites
(Wikipedia, Halachipedia, HebrewBooks, MyMemory) and hands to the model as
reference material is a second, untrusted input channel: anyone who can edit a
page or a translation-memory entry those connectors retrieve can plant
"ignore all previous instructions ..." in it. This module applies a phrase
heuristic to that text just before it enters the prompt, at the places
retrieved web text reaches the model:

  * build_prompt() -> _format_one_context_item() (the live /ask pre-fetch
    Halachipedia / general-web sections, Flask and ASGI alike), and
  * the agentic web_search / search_responsa_external / translate_text tool
    handlers in backend/ai_tools.py.

A flagged snippet is DROPPED whole (not sentence-trimmed: an attacker picks
where the payload sits, so partial trimming is trivially evadable) and only a
source label plus a marker count is logged. Neither the retrieved text nor any
user question text is ever logged (Sentry/logs must not receive raw halachic
question text -- see app.question_length_bucket()).

The heuristic is deliberately conservative, exactly like the query-side one:
a false positive silently loses a legitimate snippet. It differs from
claude.PROMPT_INJECTION_PATTERNS in two deliberate ways, both driven by that:

  * the bare phrase "you are now" is NOT a marker. It is ordinary second-person
    halachic prose ("you are now obligated to ...", "once the seder starts you
    are now permitted to ...") and would strip real Halachipedia text. Only its
    unmistakable jailbreak forms ("you are now DAN", "... unrestricted") count.
  * "ignore/disregard/forget all|any|your instructions" WITHOUT a qualifier such
    as "previous"/"prior"/"above" is a marker only when it is not followed by a
    word that names whose instructions they are ("ignore any instructions from
    his doctor", "... the doctor gives"). That is normal halachic prose (Yom
    Kippur fasting and medical guidance) and the qualified forms ("ignore all
    previous instructions") stay flagged unconditionally.

Extra markers no legitimate halachic text contains: chat-template control
tokens, the app's own <retrieved_context> delimiter (a snippet carrying the
closing tag is trying to break out of its wrapper), and the same
"ignore previous instructions" idea in Hebrew, French, Spanish, German and
Russian -- the connectors return Hebrew text, and an attacker is not obliged to
write English.

Matching runs on the text the way the model will see it: NFKC-normalized (which
also folds fullwidth and mathematical-alphabet look-alikes) with control and
format characters removed, and additionally on a copy with Cyrillic/Greek
look-alike letters folded to Latin, so "ignоre" (Cyrillic о) is caught while
genuine Russian phrases still match unfolded.

This is a leaf module (no import of backend.claude) so claude.py can import it
without a cycle; tests/test_retrieval_guard.py pins that every phrase the
query-side list detects is still detected here, except the deliberate
divergences above.

Limits (honest ones): this is a phrase heuristic, not a classifier. A
sufficiently paraphrased injection ("kindly set aside what you were told
earlier ...") is not caught, and no phrase list can close that. It is one layer
next to the structural framing -- retrieved web text is wrapped in
<retrieved_context> tags that both system prompts name as data, never
instructions -- and the output-policy review, not a replacement for them.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, List

logger = logging.getLogger(__name__)

_IGNORE_VERB = r"(?:ignore|disregard|forget|override)"
# A word that makes "instructions" unmistakably the assistant's own earlier
# ones. With one of these the phrase is flagged unconditionally.
_STRONG_QUALIFIER = r"(?:previous|prior|above|earlier|preceding|original|system)"
_DIRECTIVE_NOUN = r"(?:instructions|prompts|directives)"
# "ignore any instructions [from|of|given by|the doctor gives|...]" -- prose that
# says whose instructions they are is not addressed to the assistant.
_NAMES_WHOSE = r"(?!\s+(?:from|of|given|by|issued|provided|regarding|concerning|about|for|that|which|who|the|his|her|their|its)\b)"

# One small compiled pattern per phrase (the PROMPT_INJECTION_PATTERNS idiom;
# keeps each regex structurally trivial and linear-time -- SonarCloud
# python:S5843 / S8786). Matched against the normalized text (see _match_subjects).
RETRIEVED_INJECTION_PATTERNS = [
    # "ignore [all|any|your] previous instructions" -- always flagged.
    re.compile(rf"{_IGNORE_VERB}\s+(?:(?:all|any|your)\s+)?{_STRONG_QUALIFIER}\s+{_DIRECTIVE_NOUN}", re.IGNORECASE),
    # "ignore all|any|your [other|these] instructions" -- flagged unless it names whose.
    re.compile(rf"{_IGNORE_VERB}\s+(?:all|any|your)\s+(?:(?:other|these|those|safety)\s+)?{_DIRECTIVE_NOUN}{_NAMES_WHOSE}", re.IGNORECASE),
    # "ignore instructions from the system / developer" -- the _NAMES_WHOSE
    # exemption above must not become a way around the screen.
    re.compile(rf"{_IGNORE_VERB}\s+(?:(?:all|any|your)\s+)?{_DIRECTIVE_NOUN}\s+(?:from|of|by|given\s+by)\s+(?:the\s+|your\s+)?(?:system|developers?|operators?|users?|assistant|anthropic|openai)\b", re.IGNORECASE),
    re.compile(r"(?:ignore|disregard|forget)\s+(?:everything|all)\s+(?:above|before|previously)", re.IGNORECASE),
    re.compile(r"new\s+instructions?\s*:", re.IGNORECASE),
    re.compile(r"do\s+not\s+(?:tell|inform)\s+the\s+(?:user|reader)", re.IGNORECASE),
    re.compile(r"from\s+now\s+on,?\s+(?:only\s+)?(?:respond|answer|reply)", re.IGNORECASE),
    re.compile(r"system\s+prompt", re.IGNORECASE),
    re.compile(r"developer\s+message", re.IGNORECASE),
    re.compile(r"reveal\s+(?:your|the)\s+(?:system|internal)\s+instructions", re.IGNORECASE),
    re.compile(r"bypass\s+(?:the\s+)?(?:hierarchy|guardrails|safety)", re.IGNORECASE),
    re.compile(r"jailbreak", re.IGNORECASE),
    # The unmistakable forms of "you are now ..." only -- see the module docstring.
    re.compile(r"you\s+are\s+now\s+(?:an?\s+)?(?:dan\b|unrestricted|unfiltered|jailbroken|in\s+developer\s+mode)", re.IGNORECASE),
    # Chat-template control tokens and the app's own untrusted-data delimiter.
    re.compile(r"<\|(?:im_start|im_end|system|assistant|endoftext)\|>", re.IGNORECASE),
    re.compile(r"\[/?INST\]|<<\s*/?SYS\s*>>", re.IGNORECASE),
    re.compile(r"</?\s*retrieved_context\b", re.IGNORECASE),
    # Non-English "ignore [all] previous instructions". Hebrew needs "previous" /
    # "your" / "above" after "instructions", so "אין להתעלם מהוראות הרופא" ("one
    # must not ignore the doctor's instructions") is not a match. [םמ]: the verb
    # ends in a final mem (התעלם) in the imperative but a plain one when inflected.
    re.compile(r"התעל[םמ]\w*\s+מ(?:כל\s+)?ה?הוראות\s+ה?(?:קודמות|שלך|לעיל)"),
    re.compile(r"ignore[zr]?\s+(?:toutes\s+)?(?:les\s+)?instructions\s+(?:pr[ée]c[ée]dentes|ant[ée]rieures)", re.IGNORECASE),
    re.compile(r"ignora[rd]?\s+(?:todas\s+)?(?:las\s+)?instrucciones\s+(?:anteriores|previas)", re.IGNORECASE),
    re.compile(r"ignorier(?:e|en)?\s+(?:alle\s+)?(?:vorherigen|bisherigen|fr[üu]heren)\s+(?:anweisungen|instruktionen)", re.IGNORECASE),
    re.compile(r"игнорируй(?:те)?\s+(?:все\s+)?(?:предыдущие|прежние)\s+(?:инструкции|указания)", re.IGNORECASE),
]

# Cyrillic / Greek letters that render like a Latin one. Used ONLY to build a
# second matching copy of the text (never returned): NFKC does not fold these.
_CONFUSABLES = str.maketrans({
    # Cyrillic
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x",
    "і": "i", "ј": "j", "ѕ": "s", "һ": "h", "ԁ": "d", "к": "k", "м": "m", "т": "t",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
    "Р": "P", "С": "C", "Т": "T", "Х": "X", "І": "I", "Ѕ": "S", "Ј": "J",
    # Greek
    "ο": "o", "α": "a", "ε": "e", "ν": "v", "ι": "i", "ρ": "p", "τ": "t",
    "υ": "u", "κ": "k", "ϲ": "c",
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K",
    "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
})

_MAX_DEPTH = 6
_KEEP_CONTROL = frozenset("\t\n\r")


def _normalize(text: str) -> str:
    """Matching-only normalization. _sanitize_prompt_payload / _sanitize_model_output
    later strip hidden Unicode from what the model sees, so a payload split with
    zero-width characters ("ig" + U+200B + "nore") becomes intact AFTER sanitization --
    the screen therefore has to look at the text the way the model will: NFKC
    (fullwidth/compat/mathematical-alphabet forms -> plain) with control/format
    characters removed. The result is only ever matched against, never returned."""
    folded = unicodedata.normalize("NFKC", text)
    return "".join(
        ch for ch in folded
        if ch in _KEEP_CONTROL or unicodedata.category(ch) not in ("Cc", "Cf")
    )


def _match_subjects(text: str) -> tuple:
    """The normalized text, plus a copy with Cyrillic/Greek look-alikes folded to
    Latin when that changes anything. Both are searched: folding would break the
    Russian pattern, and not folding would miss "ignоre" spelled with a Cyrillic о."""
    normalized = _normalize(text)
    folded = normalized.translate(_CONFUSABLES)
    return (normalized,) if folded == normalized else (normalized, folded)


def find_injection_markers(text: str) -> List[str]:
    """Sorted, de-duplicated (lower-cased) injection-marker phrases found in
    `text`. Mirrors claude._extract_prompt_injection_markers()."""
    return sorted({
        m.group(0).lower()
        for subject in _match_subjects(str(text or ""))
        for pattern in RETRIEVED_INJECTION_PATTERNS
        for m in pattern.finditer(subject)
    })


def _iter_strings(value: Any, depth: int = 0):
    """Yield every string leaf of a (JSON-shaped) value. Dict *values* only:
    keys are our own field names, never retrieved text."""
    if isinstance(value, str):
        yield value
    elif depth >= _MAX_DEPTH:
        return
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item, depth + 1)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _iter_strings(item, depth + 1)


def count_injection_markers(value: Any) -> int:
    """Total distinct injection markers across every string in `value`
    (a string, or a dict/list nesting strings). 0 means clean."""
    markers = set()
    for text in _iter_strings(value):
        markers.update(find_injection_markers(text))
    return len(markers)


def withhold_injected(value: Any, *, source: str) -> Any:
    """Return `value` unchanged when it is clean, or None when any string in
    it carries an injection marker (the caller drops the whole snippet).

    `source` is a short fixed label chosen by the calling code ("Halachipedia",
    "web_search", ...). Only that label and the marker COUNT are logged --
    never the retrieved text, the markers themselves, or the user's question.
    """
    marker_count = count_injection_markers(value)
    if not marker_count:
        return value
    logger.warning(
        "retrieval_guard: withheld a %s snippet (%d prompt-injection marker(s))",
        source, marker_count,
    )
    return None
