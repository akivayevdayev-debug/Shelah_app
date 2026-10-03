"""Numbered source markers in a model answer.

The model ties a claim to a source by putting the source's 1-based position in
its ``sources`` array after the claim -- ``"...kindling is forbidden.[1][2]"``
-- instead of naming the source in the prose. The reader's UI turns each
marker into a small numbered chip that jumps to that source in the sources
list under the answer (static/js/citation-markers.js).

That only works if the numbers still point at the right source once the list
has been cleaned up, so the list is finalised here, in one place: empty
entries are dropped, a source named twice is kept once, the list is capped
like the conversation UI's citation list, and every marker is renumbered to
match. Markers that point at nothing are removed rather than left dangling.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

# The conversation UI shows at most this many citations
# (routes_conversations._MAX_SEED_CITATIONS, conversation-store.js), so a marker
# can never usefully point past it.
MAX_CITED_SOURCES = 6

# One marker -- "[1]", "[1, 2]", "[1,2]", "[1-3]", "[1\u20133]" -- and a run of
# adjacent ones ("[1][2]"), with the spaces before the run (never a line break)
# so a removed run doesn't leave a stray space behind. The brackets must hold digits only
# (plus separators): "[2a]" and "[the Rema]" are text, not markers.
_ONE = r"\[\d{1,2}(?:\s*[,;]\s*\d{1,2}|\s*[-\u2013]\s*\d{1,2})*\]"
_RUN_RE = re.compile(rf"([ \t]*)((?:{_ONE})+)")
_NUMBER_OR_RANGE_RE = re.compile(r"(\d{1,2})(?:\s*[-\u2013]\s*(\d{1,2}))?")
_NOTE_SPLIT_RE = re.compile(r"\s+[—–]\s+")


def source_key(source: str) -> str:
    """A source reduced to the part before its relevance note, lower-cased:
    "Genesis 1:1 — creation" and "genesis 1:1" are the same source."""
    return _NOTE_SPLIT_RE.split(str(source or ""), maxsplit=1)[0].strip().lower()


def finalize_sources(
    raw_sources: Any,
    sanitize: Callable[[str], str],
    limit: int = MAX_CITED_SOURCES,
) -> Tuple[List[str], Dict[int, Optional[int]]]:
    """``(sources, index_map)``: the cleaned list and, for each 1-based
    position in ``raw_sources``, where that entry ended up (``None`` when it
    was dropped)."""
    sources: List[str] = []
    positions: Dict[str, int] = {}
    index_map: Dict[int, Optional[int]] = {}
    for raw_index, item in enumerate(raw_sources if isinstance(raw_sources, list) else [], start=1):
        value = sanitize(str(item or ""))
        key = source_key(value)
        if not value or not key:
            index_map[raw_index] = None
        elif key in positions:
            index_map[raw_index] = positions[key]
        elif len(sources) >= limit:
            index_map[raw_index] = None
        else:
            sources.append(value)
            positions[key] = len(sources)
            index_map[raw_index] = len(sources)
    return sources, index_map


def _numbers_in(run: str) -> List[int]:
    numbers: List[int] = []
    for match in _NUMBER_OR_RANGE_RE.finditer(run):
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
        if end < start or end - start >= MAX_CITED_SOURCES:
            end = start
        numbers.extend(range(start, end + 1))
    return numbers


def remap_markers(text: str, index_map: Dict[int, Optional[int]]) -> str:
    """Rewrite every marker run in ``text`` through ``index_map``: renumbered,
    de-duplicated and sorted, one ``[n]`` per source (``[1, 2]`` becomes
    ``[1][2]``, ``[3][1]`` becomes ``[1][2]``), and removed when none of its
    sources survived."""

    def replace(match: "re.Match[str]") -> str:
        mapped: List[int] = []
        for number in _numbers_in(match.group(2)):
            new = index_map.get(number)
            if new is not None and new not in mapped:
                mapped.append(new)
        if not mapped:
            return ""
        return match.group(1) + "".join(f"[{n}]" for n in sorted(mapped))

    return _RUN_RE.sub(replace, str(text or ""))


def strip_markers(text: str) -> str:
    """``text`` without any marker -- for earlier turns shown to the model,
    whose numbers belong to an answer (and source list) it is not writing."""
    return _RUN_RE.sub("", str(text or ""))
