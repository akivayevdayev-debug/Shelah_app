"""The checked-in siddur (data/siddur/<rite>/, built by scripts/build_siddur.py).

Read-only lookups over files that only change with a deploy, so each file is
parsed once per process and kept (a warm serverless instance serves every
later request from memory, no disk read).
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "siddur"
DEFAULT_RITE = "edot-hamizrach"
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _is_slug(value: str) -> bool:
    return bool(value) and bool(_SLUG_RE.match(value))


@lru_cache(maxsize=8)
def get_toc(rite: str) -> dict | None:
    if not _is_slug(rite):
        return None
    path = DATA_DIR / rite / "toc.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=64)
def get_service(rite: str, service: str) -> dict | None:
    if get_toc(rite) is None or not _is_slug(service):
        return None
    path = DATA_DIR / rite / "services" / f"{service}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def iter_services(rite: str = DEFAULT_RITE):
    """(occasion, service) pairs in table-of-contents order."""
    toc = get_toc(rite) or {"occasions": []}
    for occasion in toc["occasions"]:
        for service in occasion["services"]:
            yield occasion, service


def find(rite: str, service: str, section: str | None = None) -> tuple[dict, dict | None] | None:
    """(service entry, section entry) from the table of contents, or None.

    With no ``section``, the section is None for a multi-section service and
    the lone section for a one-section service. A one-section service has no
    separate section URL, so naming its section is a miss."""
    for _occasion, entry in iter_services(rite):
        if entry["slug"] != service:
            continue
        sections = entry["sections"]
        if section is None:
            return entry, (sections[0] if len(sections) == 1 else None)
        if len(sections) == 1:
            return None
        for sec in sections:
            if sec["slug"] == section:
                return entry, sec
        return None
    return None


def siddur_path(rite: str, service: str | None = None, section: str | None = None) -> str:
    parts = ["siddur", rite] + [p for p in (service, section) if p]
    return "/" + "/".join(parts)


def find_by_ref(ref: str, rite: str = DEFAULT_RITE) -> tuple[dict, dict] | None:
    """The TOC entry whose Sefaria ref is ``ref`` (or its segment, "<ref> 5")."""
    wanted = str(ref or "").strip()
    for _occasion, entry in iter_services(rite):
        for sec in entry["sections"]:
            if wanted == sec["ref"] or wanted.startswith(sec["ref"] + " "):
                return entry, sec
    return None


def search_services(query: str, rite: str = DEFAULT_RITE) -> tuple[dict, dict | None] | None:
    """Best TOC match for a free-text prayer name ("Shacharit", "ערבית",
    "weekday arvit", "Amida of Mincha", "Shema"), as (service, section or
    None), or None when nothing matches well enough.

    Matching is on word sets, case-, punctuation- and common-spelling-
    insensitive. A name every query word is part of beats a name that is
    part of the query, and the name the query covers most closely wins, so
    "Shema" is Shacharit's Shema section, not the bedtime Shema service;
    ties go to the service, then to table-of-contents order."""
    wanted = _words(query)
    if not wanted:
        return None
    best, best_score = None, (0, 0.0, 0)
    for _occasion, entry in iter_services(rite):
        title, gloss = entry["title"], entry.get("gloss", "")
        names = [entry["slug"], title["en"], title["he"], gloss, f"{title['en']} {gloss}"]
        candidates = [((entry, None), names, 1)]
        for sec in entry["sections"]:
            sec_names = [sec["slug"], sec["title"]["en"], sec["title"]["he"]]
            sec_names += [f"{name} {service_name}" for name in sec_names[1:] for service_name in names[1:4]]
            candidates.append(((entry, sec), sec_names, 0))
        for match, candidate_names, is_service in candidates:
            for name in candidate_names:
                level, ratio = _match(wanted, _words(name))
                score = (level, ratio, is_service)
                if level and score > best_score:
                    best, best_score = match, score
    return best


# Other spellings of the names the siddur uses ("Arbit", "Alenu", "Amida").
_SPELLINGS = {"arvit": "arbit", "maariv": "arbit", "aleinu": "alenu", "esrei": "esre", "esreh": "esre"}
_STOPWORDS = frozenset({"a", "an", "and", "at", "for", "in", "of", "on", "the", "to", "shel", "prayer", "service"})


def _normalize(value: str) -> str:
    return re.sub(r"[\s'\"\-_:.,()]+", " ", str(value or "").lower()).strip()


def _words(value: str) -> frozenset[str]:
    words = set()
    for word in _normalize(value).replace("ma ariv", "maariv").split():
        if word in _STOPWORDS:
            continue
        word = _SPELLINGS.get(word, word)
        if len(word) > 3 and word.endswith("ah"):  # Amidah/Amida, Havdalah/Havdala
            word = word[:-1]
        words.add(word)
    if {"shemoneh", "esre"} <= words or {"shmoneh", "esre"} <= words:
        words -= {"shemoneh", "shmoneh", "esre"}
        words.add("amida")
    return frozenset(words)


def _match(wanted: frozenset[str], name: frozenset[str]) -> tuple[int, float]:
    """(level, closeness): 3 = same words, 2 = the name holds every query
    word, 1 = the query holds the whole name (and the name is at least half
    of it, so "Kaddish after Mincha" isn't taken for Mincha); 0 = no match.
    An empty name (a blank gloss) is a proper subset too short to count."""
    if wanted == name:
        return 3, 1.0
    if wanted < name:
        return 2, len(wanted) / len(name)
    if name < wanted and len(name) * 2 >= len(wanted):
        return 1, len(name) / len(wanted)
    return 0, 0.0
