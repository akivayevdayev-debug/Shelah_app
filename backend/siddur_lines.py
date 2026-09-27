"""Typed lines for siddur text (Prayers audit §2.2).

Sefaria stores a siddur section as a flat list of segments, each an HTML
fragment. Nothing in that markup says "this is an instruction" or "this is
said only on Rosh Hodesh" -- but the printed-Sephardi-siddur convention the
Edot HaMizrach text follows does, typographically:

- A segment that is only ``<big>...</big>`` (optionally inside ``<b>``) is a
  **heading** ("סדר הבדלה").
- A segment wholly wrapped in ``<small>`` with **no vowel points** is an
  **instruction** / rubric ("יקח הכוס בידו הימנית..."). Nested ``<small>(...)``
  inside it is a source citation and stays part of it.
- A segment wholly wrapped in ``<small>`` **with** vowel points is
  **conditional** text: said only on some days, only with a minyan, or by
  custom (seasonal inserts, Kaddish, Zohar passages). When it opens with an
  unpointed nested ``<small>`` ("בשבועות:"), that is its **label**.
- Everything else is **prayer** text; inline ``<small>`` inside it (a psalm
  citation, "ועונים הקהל:") stays inline.

The classification is by whole-segment structure, parsed as a tree -- a
segment like ``<small>A</small> text <small>B</small>`` is prayer text with
two inline notes, not a wrapped one (a regex over the string can't tell).

Output HTML is rebuilt from the parsed tree, not passed through: only
``b``, ``i``, ``small`` and ``br`` survive (``big``/``strong`` -> ``b``,
``em`` -> ``i``), every other tag is unwrapped, and all text is escaped. So a
line's ``he``/``en`` is safe to insert as HTML without trusting the source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser

LINE_TYPES = ("heading", "instruction", "conditional", "prayer")
MAX_LABEL_CHARS = 60

# Day tags ("when"): which occasion a short rubric or label switches on, read
# from the siddur's own words. The day endpoint (backend/siddur_day.py)
# reports the same keys as active, so the reader can mark the rubric that
# applies today -- a highlight beside the text as printed, never hiding or
# rewriting it. Only short rubrics/labels are tagged: a rubric paragraph
# that merely mentions Shabbat isn't a switch.
WHEN_KEYS = (
    "rosh-chodesh", "chol-hamoed-pesach", "chol-hamoed-sukkot", "chanukah",
    "purim", "fast", "shabbat", "shabbat-shuva", "pesach", "shavuot", "sukkot",
    "shemini-atzeret", "rosh-hashana", "aseret-yemei-teshuva", "barchenu",
    "barech-alenu",
)
_WHEN_PATTERNS = (
    ("rosh-chodesh", re.compile(r"ראש[\s־-]*ח(?:ו)?דש|ר\"ח")),
    ("chol-hamoed-pesach", re.compile(r"(?:חוה(?:\"|'')מ|חול המועד)\s*(?:של\s*)?פסח")),
    ("chol-hamoed-sukkot", re.compile(r"(?:חוה(?:\"|'')מ|חול המועד)\s*(?:של\s*)?סוכות")),
    ("chanukah", re.compile(r"חנוכה")),
    # Not the tail of "(יום) הכיפורים".
    ("purim", re.compile(r"(?<!כי)פורים")),
    ("fast", re.compile(r"תענית|צום|צומות|עננו")),
    ("shabbat-shuva", re.compile(r"שבת\s*ת?שובה")),
    # Shabbat itself -- not Motzaei Shabbat, Erev Shabbat or Shabbat Shuva.
    ("shabbat", re.compile(r"(?<!מוצאי)(?<!ערב)(?:^|[\s(])[בלו]?שבת(?!\s*ת?שובה)(?:$|[\s:,.)])")),
    ("pesach", re.compile(r"(?:^|[\s,])[בלו]?פסח")),
    ("shavuot", re.compile(r"שבועות")),
    ("sukkot", re.compile(r"(?:^|[\s,])[בלו]?סוכות")),
    ("shemini-atzeret", re.compile(r"שמיני עצרת")),
    ("rosh-hashana", re.compile(r"ראש[\s־-]*השנה")),
    ("aseret-yemei-teshuva", re.compile(r"עשרת ימי תשובה|עשי\"ת")),
    # Standalone "בקיץ:" / "בחורף:" rubrics only precede the two forms of
    # Birkat HaShanim (all three weekday Amidot), so they follow the rain
    # request's calendar, not Mashiv HaRuach's.
    ("barchenu", re.compile(r"^בקיץ:?$")),
    ("barech-alenu", re.compile(r"^בחורף:?$")),
)
# A "חול המועד" rubric naming neither festival applies to both.
_CHOL_HAMOED_RE = re.compile(r"חוה(?:\"|'')מ|חול המועד")
# "…אין אומרים", "…שאינו שבת", "מלבד…": a rubric saying when NOT to say
# something isn't a switch for that day.
_NEGATION_RE = re.compile(r"אין|שאינו|אינו|מלבד|(?:^|\s)לא(?:\s|$)")


def when_tags(text: str) -> list[str]:
    """Day tags for one short rubric or label (see _WHEN_PATTERNS)."""
    text = (text or "").strip()
    if not text or len(text) > MAX_LABEL_CHARS or _NEGATION_RE.search(text):
        return []
    tags = [key for key, pattern in _WHEN_PATTERNS if pattern.search(text)]
    if _CHOL_HAMOED_RE.search(text) and not any(t.startswith("chol-hamoed") for t in tags):
        tags += ["chol-hamoed-pesach", "chol-hamoed-sukkot"]
    return tags

# Hebrew vowel points (sheva..qubuts, dagesh/mappiq, shin/sin dots, qamats
# qatan). Cantillation marks (U+0591-U+05AF) are deliberately excluded: an
# unpointed rubric never has them either, and a pointed prayer always has
# vowels, so vowels alone decide it.
_NIQQUD_RE = re.compile("[ְ-ׇּׁׂ]")
_WS_RE = re.compile(r"\s+")

_TAG_MAP = {"b": "b", "strong": "b", "big": "b", "i": "i", "em": "i", "small": "small"}
_VOID_TAGS = {"br"}


@dataclass
class _Node:
    tag: str | None  # None for the fragment root
    children: list = field(default_factory=list)  # _Node | str


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node(None)
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        if tag in _VOID_TAGS:
            self._stack[-1].children.append(_Node(tag))
            return
        node = _Node(tag)
        self._stack[-1].children.append(node)
        self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self._stack[-1].children.append(_Node(tag))

    def handle_endtag(self, tag):
        # Close the nearest open element with this tag; a stray close tag
        # with no open match is ignored (Sefaria has a few).
        for depth in range(len(self._stack) - 1, 0, -1):
            if self._stack[depth].tag == tag:
                del self._stack[depth:]
                return

    def handle_data(self, data):
        self._stack[-1].children.append(data)


def _parse(fragment: str) -> _Node:
    builder = _TreeBuilder()
    builder.feed(fragment or "")
    builder.close()
    return builder.root


def _text(node) -> str:
    if isinstance(node, str):
        return node
    if node.tag == "br":
        return " "
    return "".join(_text(child) for child in node.children)


def plain_text(node) -> str:
    """Whitespace-collapsed text of a node (or fragment string)."""
    if isinstance(node, str) and not isinstance(node, _Node):
        node = _parse(node)
    return _WS_RE.sub(" ", _text(node).replace(" ", " ")).strip()


def _render_nodes(nodes) -> str:
    out = []
    for node in nodes:
        if isinstance(node, str):
            out.append(escape(node, quote=False))
        elif node.tag == "br":
            out.append("<br>")
        else:
            inner = _render_nodes(node.children)
            tag = _TAG_MAP.get(node.tag)
            out.append(f"<{tag}>{inner}</{tag}>" if tag and inner.strip() else inner)
    return "".join(out)


_EDGE_BR_RE = re.compile(r"^(?:\s*<br>)+|(?:<br>\s*)+$")


def _render(nodes) -> str:
    # Whitespace is collapsed once, over the whole fragment: trimming inside
    # each inline tag would glue "<small>ועונים הקהל: </small>בָּרוּךְ" together.
    html = _WS_RE.sub(" ", _render_nodes(nodes).replace(" ", " ")).strip()
    return _EDGE_BR_RE.sub("", html).strip()


def _significant(children) -> list:
    return [c for c in children if not (isinstance(c, str) and not c.strip())]


def _unwrap_bold(node: _Node) -> _Node:
    """Descend through a lone ``<b>``/``<strong>`` wrapper, so ``<b><small>…``
    and ``<b><big><b>…`` classify by what's inside."""
    while True:
        kids = _significant(node.children)
        if len(kids) == 1 and isinstance(kids[0], _Node) and kids[0].tag in ("b", "strong"):
            node = kids[0]
            continue
        return node


def _lone_child(node: _Node, tag: str) -> _Node | None:
    kids = _significant(_unwrap_bold(node).children)
    if len(kids) == 1 and isinstance(kids[0], _Node) and kids[0].tag == tag:
        return kids[0]
    return None


def _has_tag(node, tag: str) -> bool:
    if isinstance(node, str):
        return False
    return node.tag == tag or any(_has_tag(child, tag) for child in node.children)


def _text_outside_small(nodes) -> str:
    parts = []
    for node in nodes:
        if isinstance(node, str):
            parts.append(node)
        elif node.tag != "small":
            parts.append(_text_outside_small(node.children))
    return "".join(parts)


def is_pointed(text: str) -> bool:
    return bool(_NIQQUD_RE.search(text or ""))


def classify(fragment: str) -> dict | None:
    """Type one Hebrew segment. Returns None for an empty segment, else
    ``{"t": type, "he": html}`` plus ``"label"`` (plain text) for a labelled
    conditional."""
    root = _parse(fragment)
    if not plain_text(root):
        return None

    big = _lone_child(root, "big")
    if big is not None and not _has_tag(big, "small"):
        return {"t": "heading", "he": escape(plain_text(big), quote=False)}

    small = _lone_child(root, "small")
    if small is not None:
        body = small.children
        if not is_pointed(_text_outside_small(body)):
            return {"t": "instruction", "he": _render(body)}
        line = {"t": "conditional"}
        kids = _significant(body)
        first = kids[0] if kids else None
        if isinstance(first, _Node) and first.tag == "small" and not is_pointed(_text(first)):
            label = plain_text(first)
            # A long unpointed opener is a rubric paragraph, not a label
            # chip ("On Rosh Hodesh:"): it stays in the body.
            if label and len(label) <= MAX_LABEL_CHARS:
                line["label"] = label
                body = body[body.index(first) + 1:]
        line["he"] = _render(body)
        return line

    return {"t": "prayer", "he": _render(root.children)}


def render_fragment(fragment: str) -> str:
    """Sanitized HTML for a segment that isn't typed (English)."""
    return _render(_parse(fragment).children)


def build_lines(he_segments: list, en_segments: list | None = None) -> list[dict]:
    """Typed lines for one section.

    ``n`` is the 1-based Sefaria segment number, so a line maps back to its
    ref (``<section ref> <n>``). English is paired by index; an English
    segment beside an empty Hebrew one keeps the ``prayer`` type."""
    en_segments = en_segments or []
    lines = []
    for index in range(max(len(he_segments), len(en_segments))):
        he_raw = he_segments[index] if index < len(he_segments) else ""
        en_raw = en_segments[index] if index < len(en_segments) else ""
        he_raw = he_raw if isinstance(he_raw, str) else ""
        en_raw = en_raw if isinstance(en_raw, str) else ""
        line = classify(he_raw)
        en_html = render_fragment(en_raw)
        if line is None:
            if not en_html:
                continue
            line = {"t": "prayer", "he": ""}
        if en_html:
            line["en"] = escape(plain_text(en_raw), quote=False) if line["t"] == "heading" else en_html
        if line["t"] in ("instruction", "conditional"):
            tags = when_tags(line.get("label") or plain_text(line["he"]))
            if tags:
                line["when"] = tags
        line["n"] = index + 1
        lines.append(line)
    return lines


def english_is_aligned(he_segments: list, en_segments: list | None) -> bool:
    """English is paired with Hebrew by index, so it can't run past the
    Hebrew's last segment -- when it does (Sefaria's "Kaveh"), the two
    versions number that section differently and pairing would mismatch
    every line."""
    last = max((i for i, s in enumerate(en_segments or []) if isinstance(s, str) and s.strip()), default=-1)
    return last < len(he_segments or [])
