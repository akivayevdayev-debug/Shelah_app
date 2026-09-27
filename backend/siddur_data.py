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
    "weekday arvit", "Amida of Mincha"): a service whose name, gloss or Hebrew
    title matches, else a section. Case- and punctuation-insensitive."""
    norm = _normalize(query)
    if not norm:
        return None
    services = list(iter_services(rite))
    for _occasion, entry in services:
        names = (entry["slug"], entry["title"]["en"], entry["title"]["he"], entry.get("gloss", ""))
        if any(_normalize(name) == norm for name in names):
            return entry, None
    for _occasion, entry in services:
        names = (entry["title"]["en"], entry["title"]["he"], entry.get("gloss", ""), entry["slug"])
        if any(norm in _normalize(name) or (_normalize(name) and _normalize(name) in norm) for name in names):
            return entry, None
    for _occasion, entry in services:
        for sec in entry["sections"]:
            if norm in (_normalize(sec["title"]["en"]), _normalize(sec["title"]["he"]), _normalize(sec["slug"])):
                return entry, sec
    return None


def _normalize(value: str) -> str:
    return re.sub(r"[\s'\"\-_:.,()]+", " ", str(value or "").lower()).strip()
