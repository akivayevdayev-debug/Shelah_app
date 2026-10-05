"""
Community blueprint for Sh'elah.

Community customs (Merkava) knowledge and interaction routes extracted verbatim
from ``app.py`` (Stage 2 blueprint split). Logic is unchanged; only the route
decorator target moved from ``@app.route`` to ``@routes_community.route`` and
shared helpers/constants are imported from ``app``.

Note: ``app.py`` resolved the ``customs/`` data directory relative to its own
``__file__`` (the project root). Because this module lives one level deeper in
``backend/``, ``_PROJECT_ROOT`` is computed explicitly so the resolved data path
is byte-for-byte identical to the original lookup.
"""

import json
import logging
import os
from urllib.parse import unquote

from flask import Blueprint, jsonify

from backend.helpers import COMMUNITIES, _canonicalize_community_name
from app import _build_trusted_custom_sources

routes_community = Blueprint("community", __name__)
logger = logging.getLogger(__name__)

# Project root (one level above backend/) — matches app.py's __file__ location,
# so os.path.join(_PROJECT_ROOT, "customs", ...) resolves to the same path.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Fields kept for the people maintaining the data, not for the browser: an
# entry's provenance note (what was and was not opened) and the file's queue of
# questions for a rabbi, which is phrased for maintainers.
_REVIEWER_ONLY_ENTRY_KEYS = frozenset({"review_notes"})
_REVIEWER_ONLY_FILE_KEYS = frozenset({"needs_rabbinic_review"})


def _without_keys(item, keys):
    return {k: v for k, v in item.items() if k not in keys} if isinstance(item, dict) else item


def _public_community_data(data):
    """The community file as it may be sent to a browser: reviewer-only fields
    removed from the file, its halacha_index entries and its distinctive customs."""
    if not isinstance(data, dict):
        return data
    public = _without_keys(data, _REVIEWER_ONLY_FILE_KEYS)
    for key in ("halacha_index", "unique_minhagim"):
        if isinstance(data.get(key), list):
            public[key] = [_without_keys(item, _REVIEWER_ONLY_ENTRY_KEYS) for item in data[key]]
    return public


def _text_list(value):
    """A list of non-blank strings from a field that should be one."""
    return [str(item).strip() for item in value if str(item or "").strip()] if isinstance(value, list) else []


def _hebrew(item, key):
    """The hand-written Hebrew for a field ("<key>_he"), or "" when the entry has none."""
    return str(item.get(f"{key}_he") or "").strip() if isinstance(item, dict) else ""


def _distinctive_customs(data):
    """The community's distinctive customs (unique_minhagim) as display entries.
    The legacy {examples, notes} dict shape carries no per-custom entries."""
    entries = []
    for item in data.get("unique_minhagim") if isinstance(data.get("unique_minhagim"), list) else []:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            continue
        entries.append({
            "name": str(item["name"]).strip(),
            "name_he": _hebrew(item, "name"),
            "description": str(item.get("description") or "").strip(),
            "description_he": _hebrew(item, "description"),
            "when": str(item.get("when") or "").strip(),
            "source": str(item.get("source") or "").strip(),
            "source_url": str(item.get("source_url") or "").strip(),
            "confidence": str(item.get("confidence") or "").strip(),
        })
    return entries


@routes_community.route("/api/communities/list")
def get_communities_list():
    """Returns list of available communities."""
    communities = sorted(COMMUNITIES.keys())
    return jsonify([{"name": c} for c in communities])


@routes_community.route("/api/community/<name>")
def get_community(name):
    """Returns community customs data."""
    resolved_name = (unquote(name or "") or "").strip()
    canonical_name = _canonicalize_community_name(resolved_name)
    if canonical_name is None:
        return jsonify({"error": f"Community '{resolved_name}' not found"}), 404

    filename = COMMUNITIES[canonical_name]
    filepath = os.path.join(_PROJECT_ROOT,
                            "customs", f"{filename}.json")

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = _public_community_data(json.load(f))

        # Extract key information for display
        identity = data.get("identity", {})
        trusted_sources = _build_trusted_custom_sources(data)

        # Extract customs from halacha_index
        customs_content = {}
        for item in data.get("halacha_index", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            topic = item.get("topic", "")
            category = item.get("category", "")
            key = f"{category}_{topic}".lower().strip("_")
            # The key is lowercased for stable lookup; the entry keeps the data's
            # own capitalisation ("HaShatz", "Tisha B'Av") for display.
            customs_content[key] = {
                "category": category,
                "topic": topic,
                "topic_he": _hebrew(item, "topic"),
                "ruling": item.get("summary", ""),
                "ruling_he": _hebrew(item, "summary"),
                "common_practices": item.get("common_practices", []),
                "variants": item.get("variants", []),
                "confidence": item.get("confidence", ""),
                "source": item.get("source", "") or ", ".join(trusted_sources[:4]),
                "source_url": item.get("source_url", ""),
                "references": item.get("references", []),
            }

        fallback_customs = data if isinstance(data, dict) else {}

        return jsonify({
            "name": canonical_name,
            "requested_name": resolved_name,
            "heritage_id": data.get("heritage_id") if isinstance(data, dict) else None,
            "primary_origin": identity.get("primary_origin", "") if isinstance(identity, dict) else "",
            "major_authorities": trusted_sources,
            "distinctive_customs": _distinctive_customs(data) if isinstance(data, dict) else [],
            "disputes": _text_list(data.get("disputes")) if isinstance(data, dict) else [],
            "gaps": _text_list(data.get("gaps")) if isinstance(data, dict) else [],
            "customs": customs_content if customs_content else fallback_customs,
            "raw_data": data  # Full data available if needed
        })
    except Exception as e:
        logger.exception("Could not load community data for %r: %s", canonical_name, e)
        return jsonify({"error": "Could not load community data."}), 500


def _background_timeline_entries(data):
    """Timeline entries from historical_background.timeline ({period, event,
    halachic_impact} objects): the first sentence of the event is the title,
    the rest of it and the halachic impact the description."""
    background = data.get("historical_background") if isinstance(data, dict) else None
    timeline = background.get("timeline") if isinstance(background, dict) else None
    entries = []
    for item in timeline if isinstance(timeline, list) else []:
        if not isinstance(item, dict):
            continue
        event = str(item.get("event") or "").strip()
        impact = str(item.get("halachic_impact") or "").strip()
        first, _, rest = event.partition(". ")
        title = (first if rest else event.rstrip(".")).strip()
        description = " ".join(part for part in (rest.strip(), impact) if part)
        if not title and not description:
            continue
        entries.append({
            "title": title[:120],
            "description": description[:400],
            "approx_period": str(item.get("period") or "").strip()[:80],
        })
    return entries


def _timeline_entries_from_value(key, value):
    """Normalize one community-data field into timeline entries.

    Handles the three shapes community JSON files use for these fields:
    a list of dicts, a list of plain strings, or a single string. Split out
    of get_community_timeline() to keep this shape-dispatch out of that
    route's own complexity count (SonarCloud python:S3776).
    """
    entries = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                entries.append({
                    "title": str(item.get("title") or item.get("period") or key).strip()[:120],
                    "description": str(item.get("description") or item.get("event") or "").strip()[:400],
                    "approx_period": str(item.get("year") or item.get("period") or "").strip()[:80],
                })
            elif isinstance(item, str):
                entries.append({
                    "title": key.replace("_", " ").title(),
                    "description": item.strip()[:400],
                    "approx_period": "",
                })
    elif isinstance(value, str) and value.strip():
        entries.append({
            "title": key.replace("_", " ").title(),
            "description": value.strip()[:400],
            "approx_period": "",
        })
    return entries


@routes_community.route("/api/community/<name>/timeline")
def get_community_timeline(name):
    """Returns a normalized community timeline for timeline view components."""
    resolved_name = (unquote(name or "") or "").strip()
    canonical_name = _canonicalize_community_name(resolved_name)
    if canonical_name is None:
        return jsonify({"error": f"Community '{resolved_name}' not found"}), 404

    filename = COMMUNITIES[canonical_name]
    filepath = os.path.join(_PROJECT_ROOT,
                            "customs", f"{filename}.json")
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        logger.exception("Could not load community data for %r: %s", canonical_name, e)
        return jsonify({"error": "Could not load community data."}), 500

    timeline = []

    identity = data.get("identity", {}) if isinstance(data, dict) else {}
    origin = identity.get("primary_origin") if isinstance(
        identity, dict) else ""
    if origin:
        timeline.append({
            "title": "Primary Origin",
            "description": origin,
            "approx_period": "Historic",
        })

    timeline.extend(_background_timeline_entries(data))

    for key in ("timeline", "history", "historical_timeline", "migration_story"):
        value = data.get(key) if isinstance(data, dict) else None
        timeline.extend(_timeline_entries_from_value(key, value))

    if not timeline:
        timeline.append({
            "title": "Tradition",
            "description": f"{canonical_name} customs are preserved through local minhagim and halachic practice.",
            "approx_period": "Ongoing",
        })

    return jsonify({
        "name": canonical_name,
        "events": timeline[:30],
    })


# ─── Backward-compat route alias ──────────────────────────────────────────────
@routes_community.route("/api/communities")
def api_communities_alias():
    """/api/communities → /api/communities/list (backward compat)."""
    return get_communities_list()
