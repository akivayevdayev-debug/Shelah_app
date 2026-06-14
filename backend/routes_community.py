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
import os
from urllib.parse import unquote

from flask import Blueprint, jsonify

from backend.helpers import COMMUNITIES, _canonicalize_community_name
from app import _build_trusted_custom_sources

routes_community = Blueprint("community", __name__)

# Project root (one level above backend/) — matches app.py's __file__ location,
# so os.path.join(_PROJECT_ROOT, "customs", ...) resolves to the same path.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
            data = json.load(f)

        # Extract key information for display
        identity = data.get("identity", {})
        trusted_sources = _build_trusted_custom_sources(data)

        # Extract customs from halacha_index
        customs_content = {}
        for item in data.get("halacha_index", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            topic = item.get("topic", "").lower()
            category = item.get("category", "").lower()
            key = f"{category}_{topic}".strip("_")
            customs_content[key] = {
                "category": category,
                "topic": topic,
                "ruling": item.get("summary", ""),
                "common_practices": item.get("common_practices", []),
                "source": item.get("source", "") or ", ".join(trusted_sources[:4])
            }

        fallback_customs = data if isinstance(data, dict) else {}

        return jsonify({
            "name": canonical_name,
            "requested_name": resolved_name,
            "heritage_id": data.get("heritage_id") if isinstance(data, dict) else None,
            "primary_origin": identity.get("primary_origin", "") if isinstance(identity, dict) else "",
            "customs": customs_content if customs_content else fallback_customs,
            "raw_data": data  # Full data available if needed
        })
    except Exception as e:
        return jsonify({"error": f"Could not load community data: {str(e)}"}), 500


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
        return jsonify({"error": f"Could not load community data: {str(e)}"}), 500

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

    for key in ("timeline", "history", "historical_timeline", "migration_story"):
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    timeline.append({
                        "title": str(item.get("title") or item.get("period") or key).strip()[:120],
                        "description": str(item.get("description") or item.get("event") or "").strip()[:400],
                        "approx_period": str(item.get("year") or item.get("period") or "").strip()[:80],
                    })
                elif isinstance(item, str):
                    timeline.append({
                        "title": key.replace("_", " ").title(),
                        "description": item.strip()[:400],
                        "approx_period": "",
                    })
        elif isinstance(value, str) and value.strip():
            timeline.append({
                "title": key.replace("_", " ").title(),
                "description": value.strip()[:400],
                "approx_period": "",
            })

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
