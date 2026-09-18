"""
Prayers blueprint for Sh'elah.

Prayer-book listing, previews, and full siddur text routes extracted verbatim
from ``app.py`` (Stage 2 blueprint split). Prayer content is fetched live from
Sefaria refs listed in ``SIDDUR_SECTION_MAP``. Logic is unchanged; only the
route decorator target moved from ``@app.route`` to ``@routes_prayers.route``
and shared helpers/constants are imported from ``app``.
"""

from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote

from flask import Blueprint, jsonify

from app import SIDDUR_SECTION_MAP, _get_prayer_refs
from backend.logging_setup import submit_with_context

routes_prayers = Blueprint("prayers", __name__)

# Bounded worker pool for fetching per-ref siddur text from Sefaria in
# parallel. Kept deliberately small (not unbounded) because Sefaria applies
# its own upstream rate limiting / Cloudflare blocking (see
# backend/sefaria_library.py's ``_sefaria_block_status``) -- firing 80
# requests at once would be more likely to trip that than to help.
_SIDDUR_TEXT_FETCH_WORKERS = 6


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

    en_preview = "\n".join([line.get("en", "") for line in preview.get(
        "lines", []) if line.get("en")][:8]).strip()
    he_preview = "\n".join([line.get("he", "") for line in preview.get(
        "lines", []) if line.get("he")][:8]).strip()
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

    # Fetch every ref's text in parallel (bounded pool) instead of one at a
    # time -- on a cold cache, up to 80 sequential Sefaria round-trips could
    # take up to a full minute. Results are mapped back by index so ordering
    # matches the original ref order regardless of completion order.
    results = [None] * len(refs)
    worker_count = min(_SIDDUR_TEXT_FETCH_WORKERS, len(refs))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_index = {
            submit_with_context(executor, get_text, ref): idx
            for idx, ref in enumerate(refs)
        }
        for future in future_to_index:
            idx = future_to_index[future]
            try:
                results[idx] = future.result()
            except Exception:
                # Preserve the same per-ref tolerance as the original
                # sequential loop: one ref failing must not fail the batch.
                results[idx] = {"error": "fetch failed"}

    combined_lines = []
    for ref, data in zip(refs, results):
        data = data or {}
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
