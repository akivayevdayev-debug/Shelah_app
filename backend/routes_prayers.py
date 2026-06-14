"""
Prayers blueprint for Sh'elah.

Prayer-book listing, previews, and full siddur text routes extracted verbatim
from ``app.py`` (Stage 2 blueprint split). Prayer content is fetched live from
Sefaria refs listed in ``SIDDUR_SECTION_MAP``. Logic is unchanged; only the
route decorator target moved from ``@app.route`` to ``@routes_prayers.route``
and shared helpers/constants are imported from ``app``.
"""

from urllib.parse import unquote

from flask import Blueprint, jsonify

from app import SIDDUR_SECTION_MAP, _get_prayer_refs

routes_prayers = Blueprint("prayers", __name__)


@routes_prayers.route("/api/prayers/list")
def get_prayers_list():
    """Returns all prayer books from Sefaria Liturgy plus legacy quick services."""
    from backend.sefaria_library import get_liturgy_books

    items = []
    seen = set()

    for name in SIDDUR_SECTION_MAP.keys():
        items.append({"name": name, "title": name, "source": "legacy-service"})
        seen.add(name)

    for book in get_liturgy_books(max_items=200):
        title = book.get("title")
        if title and title not in seen:
            items.append({"name": title, "title": title,
                         "source": "sefaria-liturgy"})
            seen.add(title)

    return jsonify(items)


@routes_prayers.route("/api/prayer/<name>")
def get_prayer(name):
    """Returns prayer-book preview content in English and Hebrew."""
    from backend.sefaria_library import get_text

    resolved_name = (unquote(name or "") or "").strip()
    refs = _get_prayer_refs(resolved_name)
    if not refs:
        return jsonify({"error": f"Prayer '{resolved_name}' not found"}), 404

    preview = None
    for ref in refs[:12]:
        data = get_text(ref)
        if "error" not in data and (data.get("he") or data.get("en")):
            preview = data
            break

    if not preview:
        return jsonify({"error": f"Could not load prayer '{resolved_name}' from Sefaria"}), 404

    en_preview = "\n".join([l.get("en", "") for l in preview.get(
        "lines", []) if l.get("en")][:8]).strip()
    he_preview = "\n".join([l.get("he", "") for l in preview.get(
        "lines", []) if l.get("he")][:8]).strip()
    if not en_preview:
        en_preview = f"Preview available in Hebrew for {resolved_name}."
    if not he_preview:
        he_preview = f"תצוגה מקדימה זמינה באנגלית עבור {resolved_name}."

    prayer_data = {
        "en": en_preview,
        "he": he_preview,
    }

    return jsonify({
        "name": resolved_name,
        "title": resolved_name,
        "content": prayer_data,
        "languages": ["en", "he"]
    })


@routes_prayers.route("/api/siddur/full/<path:prayer_name>")
def get_siddur_full(prayer_name):
    """Fetch full prayer text from Sefaria for any supported prayer service/book."""
    from backend.sefaria_library import get_text

    resolved_name = (unquote(prayer_name or "") or "").strip()
    refs = _get_prayer_refs(resolved_name)
    if not refs:
        return jsonify({"error": f"No Sefaria mapping for '{resolved_name}'"}), 404

    combined_lines = []
    for ref in refs:
        data = get_text(ref)
        if "error" not in data and (data.get("he") or data.get("en")):
            section_title = ref.split(", ")[-1] if ", " in ref else ref
            he_title = data.get("heTitle", section_title)
            combined_lines.append({
                "he": f"<strong class='text-navy'>{he_title}</strong>",
                "en": f"<strong class='text-navy'>{section_title}</strong>",
                "type": "header"
            })
            combined_lines.extend(data.get("lines", []))

    if not combined_lines:
        return jsonify({"error": "Could not fetch prayer text from Sefaria"}), 404

    return jsonify({
        "prayer": resolved_name,
        "lines": combined_lines,
        "sources": refs
    })
