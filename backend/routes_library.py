"""
Library blueprint for Sh'elah.

Library tree, text-pane, search, word-meaning, export, and texts-index routes
extracted verbatim from ``app.py`` (Stage 2 blueprint split). Logic is
unchanged; only the route decorator target moved from ``@app.route`` to
``@routes_library.route`` and shared helpers/constants are imported from
``app``.
"""

import io
import re

from flask import Blueprint, jsonify, request, send_file

from backend.helpers import (
    QUICK_TEXT_ALIASES,
    COMMUNITIES,
    COMMUNITY_ALIASES,
    _decode_route_ref,
    _coerce_int,
    _extract_search_metadata_filters,
    _fill_missing_english_lines,
    _contains_hebrew_letters,
    _lookup_hebrew_word_meaning,
    _lookup_english_word_meaning,
    _translate_english_text_online,
    _collect_word_meaning_alternatives,
    _is_machine_translated_source,
)
from backend.auth import maybe_require_clerk_auth

routes_library = Blueprint("library", __name__)

# Sefaria wholeRef/ref strings are short, well-formed citation strings (e.g.
# "Berakhot 2a:1-13a:15"). An unanchored re.search(r'(\d+[ab])', ...) is
# O(n^2) on adversarial all-digit input with no trailing a/b: it retries the
# same greedy \d+ scan from every start position. Every number here is
# therefore bounded to 9 digits (\d{1,9}; no daf or siman has more), so
# an attempt costs a constant number of steps wherever it starts, and (?<!\d)
# keeps a match from starting in the middle of a digit run. For every input
# whose digit runs are all within the bound the results are identical to the
# old unbounded patterns; a longer run is not a citation number and simply
# does not match (SonarCloud python:S8786; tests/test_regex_linear_time.py).
# The segment-length cap below stays as defence in depth.
_MAX_REF_SEGMENT_LEN = 500
_DAF_RANGE_RE = re.compile(r'(?<!\d)(\d{1,9}[ab])[\d:]*\s*-\s*(\d{1,9}[ab])', re.IGNORECASE)
_SECTION_RANGE_RE = re.compile(r'(?<!\d)(\d{1,9})-(\d{1,9})(?!\d)')
_DAF_TOKEN_RE = re.compile(r'(?<!\d)\d{1,9}[ab]', re.IGNORECASE)


@routes_library.route("/api/library/index")
def library_index():
    """Returns report-adjusted Sefaria library tree (non-loading removals pruned, fix refs applied)."""
    from backend.sefaria_library import get_library_index
    data = get_library_index()
    return jsonify(data)


def _strip_title_prefix_from_ref(ref_body, canonical_title, index_title):
    """Strip a leading canonical- or index-title prefix (and its
    separator) from a wholeRef string, e.g. "Berakhot 2a:1-13a:15" with
    canonical_title "Berakhot" -> "2a:1-13a:15". Returns ref_body
    unchanged if neither title prefixes it. Split out of
    _parse_chapters_alt_node() (SonarCloud python:S3776).
    """
    if canonical_title and ref_body.lower().startswith(canonical_title.lower()):
        return ref_body[len(canonical_title):].lstrip(" ,")
    if index_title and ref_body.lower().startswith(index_title.lower()):
        return ref_body[len(index_title):].lstrip(" ,")
    return ref_body


def _parse_chapters_alt_node(node, entry, index_title):
    """Parse a single Chapters-alt node into a {label, fromDaf, toDaf}
    dict, or None if it doesn't carry a recognizable daf range. Split out
    of _extract_chapters_alt_sections() (SonarCloud python:S3776).
    """
    if not isinstance(node, dict):
        return None
    raw_title = str(node.get("title") or "").strip()
    canonical_title = entry.get("title", index_title)
    whole_ref = str(node.get("wholeRef") or "").strip()

    ref_body = _strip_title_prefix_from_ref(
        whole_ref, canonical_title, index_title)

    if len(ref_body) > _MAX_REF_SEGMENT_LEN:
        return None
    range_m = _DAF_RANGE_RE.search(ref_body)
    if not range_m:
        return None
    from_daf = range_m.group(1).lower()
    to_daf = range_m.group(2).lower()

    # Clean the chapter label: "Chapter 1; MeEimatai" → "MeEimatai (2a–13a)"
    # Keep the human name after the semicolon, if present
    if ';' in raw_title:
        label = raw_title.split(';', 1)[1].strip()
    else:
        label = raw_title

    return {
        "label": label,
        "fromDaf": from_daf,
        "toDaf": to_daf,
    }


def _extract_chapters_alt_sections(index_title, entry, chapters_alt):
    """Talmud daf-range groupings from entry['alts']['Chapters'|'chapters'].
    Returns [{label, fromDaf, toDaf}, ...]. Split out of
    _extract_index_sections() (SonarCloud python:S3776) -- see
    _extract_topic_alt_sections.
    """
    nodes = chapters_alt.get("nodes")
    if not isinstance(nodes, list):
        return []

    sections = []
    for node in nodes:
        section = _parse_chapters_alt_node(node, entry, index_title)
        if section is not None:
            sections.append(section)

    return sections


def _parse_topic_alt_node(node):
    """Parse a single Topic-alt node into a {label, heLabel, fromSection,
    toSection} dict, or None if it doesn't carry a recognizable section
    range. Split out of _extract_topic_alt_sections() (SonarCloud
    python:S3776).
    """
    if not isinstance(node, dict):
        return None
    label = str(node.get("title") or "").strip()
    he_label = str(node.get("heTitle") or "").strip()
    ref_body = str(node.get("wholeRef") or "").strip()
    if not label or not ref_body:
        return None

    if len(ref_body) > _MAX_REF_SEGMENT_LEN:
        return None
    range_m = _SECTION_RANGE_RE.search(ref_body)
    if not range_m:
        return None

    return {
        "label": label,
        "heLabel": he_label,
        "fromSection": int(range_m.group(1)),
        "toSection": int(range_m.group(2)),
    }


def _extract_topic_alt_sections(topic_alt):
    """Halakhic siman-range groupings from entry['alts']['Topic'|'topic'].
    Returns [{label, heLabel, fromSection, toSection}, ...]. Split out of
    _extract_index_sections() (SonarCloud python:S3776) -- see
    _extract_chapters_alt_sections.
    """
    nodes = topic_alt.get("nodes")
    if not isinstance(nodes, list):
        return []

    sections = []
    for node in nodes:
        section = _parse_topic_alt_node(node)
        if section is not None:
            sections.append(section)

    return sections


def _extract_index_sections(index_title, entry):
    """
    Extract named section groupings from a Sefaria index entry's alt
    structures, to power grouped section-grid rendering.

    Two supported shapes:
      - Talmud: entry['alts']['Chapters'|'chapters'] -- daf ranges
        parsed from each node's wholeRef (e.g. "Berakhot 2a:1-13a:15").
        Returns [{label, fromDaf, toDaf}, ...] (unchanged from before).
      - Halakhic works with siman-range groupings (Shulchan Arukh,
        Mishneh Torah, Aruch HaShulchan, Mishnah Berurah, Kitzur
        Shulchan Arukh, etc.): entry['alts']['Topic'] -- ready-made
        English/Hebrew section titles with numeric siman ranges parsed
        from each node's wholeRef (e.g.
        "Shulchan Arukh, Orach Chayim 1-7"). Returns
        [{label, heLabel, fromSection, toSection}, ...]. Field names are
        deliberately generic (not fromDaf/toDaf) since these are siman
        numbers, not daf pages.

    Returns [] when neither alt-structure is present/usable.
    """
    alts = entry.get("alts") if isinstance(entry, dict) else None
    if not isinstance(alts, dict):
        return []

    chapters_alt = alts.get("Chapters") or alts.get("chapters")
    if isinstance(chapters_alt, dict):
        return _extract_chapters_alt_sections(index_title, entry, chapters_alt)

    topic_alt = alts.get("Topic") or alts.get("topic")
    if isinstance(topic_alt, dict):
        return _extract_topic_alt_sections(topic_alt)

    return []


def _compact_talmud_leaf_ref(ref_value, normalized_title, seen):
    """Parse a single Talmud leaf ref into its compacted daf form
    ("<title> <daf>"), or None if it's blank, oversized, has no daf
    token, or its daf was already seen. Adds the daf to `seen` in place
    when a new one is found. Split out of _collapse_talmud_leaf_refs()
    (SonarCloud python:S3776).
    """
    ref_text = str(ref_value or "").strip()
    if not ref_text:
        return None

    body = ref_text
    if normalized_title and body.lower().startswith(normalized_title.lower()):
        body = body[len(normalized_title):].lstrip(" ,")

    if len(body) > _MAX_REF_SEGMENT_LEN:
        return None
    daf_match = _DAF_TOKEN_RE.search(body)
    if not daf_match:
        return None

    daf = daf_match.group(0).lower()
    if daf in seen:
        return None
    seen.add(daf)

    return f"{normalized_title} {daf}".strip()


def _collapse_talmud_leaf_refs(index_title, refs, max_items=260):
    """Convert segment-level Talmud refs into unique daf refs for stable
    grid rendering. Moved to module level (out of library_leaf_refs())
    since a nested closure's own branches count against the enclosing
    function's complexity, but a top-level function's don't
    (SonarCloud python:S3776).
    """
    if not isinstance(refs, list) or not refs:
        return []

    normalized_title = str(index_title or "").strip()
    compact_refs = []
    seen = set()

    for ref_value in refs:
        compact_ref = _compact_talmud_leaf_ref(
            ref_value, normalized_title, seen)
        if compact_ref is None:
            continue
        compact_refs.append(compact_ref)
        if len(compact_refs) >= max_items:
            break

    return compact_refs


def _schema_list_field(schema, field_name):
    """Return schema[field_name] if it's a list, else []. Split out of
    _parse_section_schema_for_synthesis() (SonarCloud python:S3776).
    """
    value = schema.get(field_name)
    return value if isinstance(value, list) else []


def _first_lowered_token(values):
    """Return the first item of `values`, stripped and lowercased, or ""
    if `values` is empty. Split out of
    _parse_section_schema_for_synthesis() (SonarCloud python:S3776).
    """
    return str(values[0] or "").strip().lower() if values else ""


def _parse_section_schema_for_synthesis(entry):
    """Validate and extract the schema fields _synthesize_section_refs()
    needs (first_level_count, first_section_name, first_address_type), or
    None if the index entry's schema doesn't support ref synthesis. Split
    out of _synthesize_section_refs() (SonarCloud python:S3776).
    """
    schema = entry.get("schema", {}) if isinstance(entry, dict) else {}
    if not isinstance(schema, dict):
        return None

    lengths = _schema_list_field(schema, "lengths")
    if not lengths:
        return None

    try:
        first_level_count = int(lengths[0])
    except (TypeError, ValueError):
        return None

    if first_level_count <= 1:
        return None

    first_section_name = _first_lowered_token(_schema_list_field(schema, "sectionNames"))
    first_address_type = _first_lowered_token(_schema_list_field(schema, "addressTypes"))

    return first_level_count, first_section_name, first_address_type


def _synthesize_talmud_daf_refs(index_title, first_level_count, max_items):
    """Build Sefaria-style Talmud daf refs (indexing starts at 2a), up to
    max_items. Split out of _synthesize_section_refs() (SonarCloud
    python:S3776).
    """
    refs = []
    for idx in range(first_level_count):
        daf_num = (idx // 2) + 2
        side = "a" if idx % 2 == 0 else "b"
        refs.append(f"{index_title} {daf_num}{side}")
        if len(refs) >= max_items:
            break
    return refs


def _synthesize_numbered_section_refs(index_title, first_level_count, max_items):
    """Build simple "<title> <n>" refs, up to max_items. Split out of
    _synthesize_section_refs() (SonarCloud python:S3776).
    """
    refs = []
    for idx in range(1, first_level_count + 1):
        refs.append(f"{index_title} {idx}")
        if len(refs) >= max_items:
            break
    return refs


def _synthesize_section_refs(index_title, max_items=140):
    """Moved to module level (out of library_leaf_refs()) since a nested
    closure's own branches count against the enclosing function's
    complexity, but a top-level function's don't (SonarCloud
    python:S3776).
    """
    from backend.sefaria_library import get_index_entry

    entry = get_index_entry(index_title)
    parsed_schema = _parse_section_schema_for_synthesis(entry)
    if parsed_schema is None:
        return [], []
    first_level_count, first_section_name, first_address_type = parsed_schema

    if first_section_name == "daf" or first_address_type == "talmud":
        refs = _synthesize_talmud_daf_refs(index_title, first_level_count, max_items)
    else:
        refs = _synthesize_numbered_section_refs(index_title, first_level_count, max_items)

    # Populate sections for halakhic works (Shulchan Arukh, Mishneh
    # Torah, etc.) whose siman-range groupings live under alts.Topic, as
    # well as the Talmud Chapters-alt groupings.
    sections = _extract_index_sections(index_title, entry)
    return refs, sections


@routes_library.route("/api/library/leaf-refs")
def library_leaf_refs():
    """Return leaf refs for a given index title to power section-grid selectors."""
    from backend.sefaria_library import get_index_entry, get_index_leaf_refs

    requested_title = _decode_route_ref(request.args.get("title", ""))
    title = str(requested_title or "").strip()
    # Raised from 260: large halakhic works truncated real data (Orach
    # Chayim has 697 simanim, Yoreh De'ah 403, Choshen Mishpat 427). Now that
    # _extract_index_sections() groups these into labeled ranges via
    # alts.Topic, a large ref count is no longer a bare unstructured wall of
    # buttons for works where grouping data is available. Note: if grouping
    # data is unavailable for some work, the frontend still receives a large
    # flat list here and must render it gracefully (pagination/virtualized
    # grid, etc.) -- this is a frontend rendering concern, not something the
    # backend should silently truncate around.
    max_refs = _coerce_int(request.args.get("max"), 140,
                           min_value=1, max_value=800)

    if not title:
        return jsonify({"title": "", "refs": [], "sections": []})

    sections = []
    try:
        refs = get_index_leaf_refs(title, max_refs=max_refs)
    except Exception:
        refs = []

    if not isinstance(refs, list):
        refs = []

    if len(refs) <= 1:
        refs, sections = _synthesize_section_refs(title, max_items=max_refs)
    else:
        # Try to extract sections even when refs came from get_index_leaf_refs
        try:
            entry = get_index_entry(title)
            sections = _extract_index_sections(title, entry)
        except Exception:
            sections = []

    if sections:
        collapsed_refs = _collapse_talmud_leaf_refs(
            title, refs, max_items=max_refs)
        if collapsed_refs:
            refs = collapsed_refs

    return jsonify({
        "title": title,
        "refs": refs,
        "sections": sections,
    })


@routes_library.route("/api/library/popular")
def library_popular():
    """Returns curated popular texts per category."""
    from backend.sefaria_library import get_popular_texts
    return jsonify(get_popular_texts())


@routes_library.route("/api/text/", strict_slashes=False, methods=["GET"])
def get_text_missing_ref():
    """`<path:ref>` below requires a non-empty first segment, so an empty
    ref (`/api/text/` or `/api/text`) never reaches it and would otherwise
    404 as an unmatched route rather than reporting the real problem."""
    return jsonify({"error": "Missing or empty text reference."}), 400


@routes_library.route("/api/text/<path:ref>")
def get_text_inline(ref):
    """Fetches a Sefaria text inline — Hebrew + English + metadata."""
    from backend.sefaria_library import get_text
    decoded_ref = _decode_route_ref(ref)
    data = get_text(decoded_ref)

    if isinstance(data, dict) and data.get("error"):
        error_type = data.get("error_type", "")
        if error_type == "sefaria_blocked" or "blocked" in str(data.get("error", "")).lower():
            return jsonify(data), 503

    should_translate = str(request.args.get("autotranslate", "1")).strip().lower() not in {
        "0", "false", "no", "off"
    }
    if should_translate and isinstance(data, dict) and not data.get("error"):
        data = _fill_missing_english_lines(data)

    return jsonify(data)


@routes_library.route("/api/diagnostics/sefaria")
def sefaria_diagnostics():
    """Real-time availability probe for the upstream Sefaria API.
    Returns status for both the v3 and v2 endpoints, plus cached block info.
    """
    from backend.sefaria_library import check_sefaria_availability
    result = check_sefaria_availability()
    http_status = 200 if result.get("overall_available") else 503
    return jsonify(result), http_status


def _normalize_requested_lang(lang_param, word_is_hebrew):
    """Normalize the ?lang= query param to "en"/"he", forcing "en" for
    Hebrew source words (to avoid transliteration-heavy round-trips).
    Split out of get_word_meaning() (SonarCloud python:S3776).
    """
    requested_lang = str(lang_param or "en").strip().lower()
    if requested_lang not in {"en", "he"}:
        requested_lang = "en"
    if word_is_hebrew and requested_lang == "he":
        requested_lang = "en"
    return requested_lang


def _lookup_word_meaning_with_fallback_translation(raw_word, word_is_hebrew, requested_lang):
    """Look up raw_word's meaning (Hebrew or English source lookup), then
    translate an English meaning into Hebrew if the caller requested Hebrew
    output and the meaning isn't already Hebrew. Split out of
    get_word_meaning() (SonarCloud python:S3776).
    """
    if word_is_hebrew:
        meaning, source = _lookup_hebrew_word_meaning(raw_word)
    else:
        meaning, source = _lookup_english_word_meaning(raw_word)

    if meaning and requested_lang == "he" and not word_is_hebrew and not _contains_hebrew_letters(meaning):
        translated_meaning, translated_source = _translate_english_text_online(
            meaning)
        if translated_meaning:
            meaning = translated_meaning
            source_parts = [part for part in [
                source, translated_source] if part]
            source = "+".join(source_parts)

    return meaning, source


def _build_word_meaning_response(raw_word, meaning, alternatives, source, requested_lang):
    """Build the /api/word/meaning JSON response, found or not-found.
    Split out of get_word_meaning() (SonarCloud python:S3776).
    """
    if not meaning:
        return jsonify({
            "word": raw_word,
            "meaning": "",
            "alternatives": [],
            "source": "",
            "status": "not_found",
            "lang": requested_lang,
        }), 404

    return jsonify({
        "word": raw_word,
        "meaning": meaning,
        "alternatives": alternatives,
        "source": source,
        # plan.md §8.F.4 / Prompt 18 item 4: label machine-translated
        # definitions so the frontend never presents an online-translation
        # fallback as an authoritative, curated definition.
        "machine_translated": _is_machine_translated_source(source),
        "status": "ok",
        "lang": requested_lang,
    })


@routes_library.route("/api/word/meaning")
def get_word_meaning():
    """Look up a highlighted word meaning (best-effort for Hebrew and English)."""
    raw_word = str(request.args.get("word", "") or "").strip()
    if not raw_word:
        return jsonify({"error": "Missing word parameter"}), 400

    word_is_hebrew = _contains_hebrew_letters(raw_word)
    requested_lang = _normalize_requested_lang(
        request.args.get("lang", "en"), word_is_hebrew)

    meaning, source = _lookup_word_meaning_with_fallback_translation(
        raw_word, word_is_hebrew, requested_lang)

    alternatives = _collect_word_meaning_alternatives(
        raw_word=raw_word,
        primary_meaning=meaning,
        word_is_hebrew=word_is_hebrew,
    )
    if alternatives:
        meaning = alternatives[0]

    return _build_word_meaning_response(
        raw_word, meaning, alternatives, source, requested_lang)


def _chapter_export_plain_text(title, ref, lines):
    header = [str(title or "").strip(), str(ref or "").strip(), ""]
    body = []
    for idx, line in enumerate(lines, start=1):
        segment = str(line.get("segment") or idx).strip()
        he = str(line.get("he") or "").strip()
        en = str(line.get("en") or "").strip()
        body.append(f"Segment {segment}")
        if he:
            body.append(f"Hebrew: {he}")
        if en:
            body.append(f"English: {en}")
        body.append("")

    return "\n".join(header + body).strip() + "\n"


def _wrap_text_for_export(text, max_chars=96):
    value = re.sub(r"\s+", " ", str(text or "").strip())
    if not value:
        return [""]

    words = value.split(" ")
    chunks = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = word
    if current:
        chunks.append(current)

    return chunks or [value]


def _normalize_export_lines(lines):
    """Validate + normalize raw payload lines for chapter export. Split out
    of export_chapter() to keep this loop out of that route's own
    complexity count (SonarCloud python:S3776).
    """
    normalized_lines = []
    for idx, line in enumerate(lines, start=1):
        if not isinstance(line, dict):
            continue
        he = str(line.get("he") or "").strip()
        en = str(line.get("en") or "").strip()
        if not he and not en:
            continue
        normalized_lines.append({
            "segment": str(line.get("segment") or idx).strip(),
            "he": he,
            "en": en,
        })
    return normalized_lines


def _export_chapter_as_txt(title, ref, normalized_lines, file_safe):
    """The "txt" branch of export_chapter(). Split out (SonarCloud
    python:S3776) -- see _export_chapter_as_docx / _export_chapter_as_pdf.
    """
    plain_text = _chapter_export_plain_text(title, ref, normalized_lines)
    txt_buffer = io.BytesIO(plain_text.encode("utf-8"))
    txt_buffer.seek(0)
    return send_file(
        txt_buffer,
        as_attachment=True,
        download_name=f"{file_safe}.txt",
        mimetype="text/plain; charset=utf-8",
    )


def _export_chapter_as_docx(title, ref, normalized_lines, file_safe):
    """The "docx" branch of export_chapter(). Split out (SonarCloud
    python:S3776) -- see _export_chapter_as_txt / _export_chapter_as_pdf.
    """
    try:
        from docx import Document as _docx_document_cls
    except Exception:
        _docx_document_cls = None
    if _docx_document_cls is None:
        return jsonify({"error": "DOCX export is unavailable on this server"}), 503

    document = _docx_document_cls()
    document.add_heading(title or "Sh'elah Chapter", level=1)
    if ref:
        document.add_paragraph(ref)

    for idx, line in enumerate(normalized_lines, start=1):
        segment = line.get("segment") or idx
        document.add_paragraph(f"Segment {segment}")
        if line.get("he"):
            document.add_paragraph(line["he"])
        if line.get("en"):
            document.add_paragraph(line["en"])

    docx_buffer = io.BytesIO()
    document.save(docx_buffer)
    docx_buffer.seek(0)
    return send_file(
        docx_buffer,
        as_attachment=True,
        download_name=f"{file_safe}.docx",
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


def _load_reportlab():
    """Return (LETTER, canvas module) or (None, None) when reportlab is unavailable."""
    try:
        from reportlab.lib.pagesizes import LETTER as _LETTER
        from reportlab.pdfgen import canvas as _canvas
    except Exception:
        return None, None
    return _LETTER, _canvas


def _pdf_text_lines(title, ref, normalized_lines):
    """The text lines, in order, that the PDF export draws (blank strings are spacing)."""
    lines = [title or "Sh'elah Chapter"]
    if ref:
        lines.append(ref)
    lines.append("")

    for idx, line in enumerate(normalized_lines, start=1):
        lines.append(f"Segment {line.get('segment') or idx}")
        if line.get("he"):
            lines.append(f"Hebrew: {line['he']}")
        if line.get("en"):
            lines.append(f"English: {line['en']}")
        lines.append("")
    return lines


def _export_chapter_as_pdf(title, ref, normalized_lines, file_safe):
    """The "pdf" branch of export_chapter(). Split out (SonarCloud
    python:S3776) -- see _export_chapter_as_txt / _export_chapter_as_docx.
    """
    _LETTER, _canvas = _load_reportlab()
    if _canvas is None or _LETTER is None:
        return jsonify({"error": "PDF export is unavailable on this server"}), 503

    pdf_buffer = io.BytesIO()
    pdf = _canvas.Canvas(pdf_buffer, pagesize=_LETTER)
    _, height = _LETTER
    y = height - 48
    left = 42

    def draw_line(text):
        nonlocal y
        for chunk in _wrap_text_for_export(text, max_chars=96):
            if y < 48:
                pdf.showPage()
                y = height - 48
            pdf.drawString(left, y, chunk)
            y -= 14

    for text in _pdf_text_lines(title, ref, normalized_lines):
        draw_line(text)

    pdf.save()
    pdf_buffer.seek(0)
    return send_file(
        pdf_buffer,
        as_attachment=True,
        download_name=f"{file_safe}.pdf",
        mimetype="application/pdf",
    )


def _parse_export_chapter_request(payload):
    """Parse and normalize the /api/export/chapter request payload into
    (title, ref, export_format, lines). Split out of export_chapter()
    (SonarCloud python:S3776).
    """
    title = str(payload.get("title") or payload.get(
        "label") or "shelah-chapter").strip()
    ref = str(payload.get("ref") or "").strip()
    export_format = str(payload.get("format") or "txt").strip().lower()
    lines = payload.get("lines") if isinstance(
        payload.get("lines"), list) else []
    return title, ref, export_format, lines


@routes_library.route("/api/export/chapter", methods=["POST"])
@maybe_require_clerk_auth
def export_chapter():
    payload = request.get_json(silent=True) or {}
    title, ref, export_format, lines = _parse_export_chapter_request(payload)

    if export_format not in {"txt", "docx", "pdf"}:
        return jsonify({"error": "Unsupported export format"}), 400

    normalized_lines = _normalize_export_lines(lines)
    if not normalized_lines:
        return jsonify({"error": "No chapter lines available to export"}), 400

    file_safe = re.sub(r"[^a-z0-9]+", "-", title.lower()
                       ).strip("-") or "shelah-chapter"

    if export_format == "txt":
        return _export_chapter_as_txt(title, ref, normalized_lines, file_safe)
    if export_format == "docx":
        return _export_chapter_as_docx(title, ref, normalized_lines, file_safe)
    return _export_chapter_as_pdf(title, ref, normalized_lines, file_safe)


@routes_library.route("/api/library/search")
def library_search():
    """Full-text search across Sefaria texts with report-based removal/fix filtering."""
    from backend.sefaria_library import search_library
    query = request.args.get("q", "")
    size = _coerce_int(request.args.get("size"), 10, min_value=1, max_value=50)
    metadata_filters = _extract_search_metadata_filters()
    if not query:
        return jsonify([])
    results = search_library(
        query, size=size, metadata_filters=metadata_filters)
    return jsonify(results)


def _add_search_suggestion(suggestions, seen, item_type, label, value, subtitle="", score=0, label_he="", subtitle_he=""):
    """Append one suggestion if not already seen (dedup by type+value).
    Moved to module level (out of search_suggest()) since a nested
    closure's own branches count against the enclosing function's
    complexity, but a top-level function's don't (SonarCloud
    python:S3776).
    """
    key = (item_type, (value or "").lower())
    if key in seen:
        return
    seen.add(key)
    suggestions.append({
        "type": item_type,
        "label": label,
        "label_he": label_he,
        "value": value,
        "subtitle": subtitle,
        "subtitle_he": subtitle_he,
        "score": score,
    })


def _collect_community_name_suggestions(q_lower, suggestions, seen):
    """Split out of search_suggest() (SonarCloud python:S3776) -- see
    _add_search_suggestion.
    """
    for community in COMMUNITIES.keys():
        if q_lower in community.lower():
            _add_search_suggestion(suggestions, seen, "community", community, community,
                                    "Community customs", 90)


def _collect_community_alias_suggestions(q_lower, suggestions, seen):
    """Split out of search_suggest() (SonarCloud python:S3776) -- see
    _add_search_suggestion.
    """
    for alias, canonical in COMMUNITY_ALIASES.items():
        if q_lower in alias and canonical in COMMUNITIES:
            _add_search_suggestion(suggestions, seen, "community", canonical, canonical,
                                    f"Community customs (matched '{alias}')", 88)


def _collect_prayer_suggestions(q_lower, suggestions, seen, get_liturgy_books):
    """Split out of search_suggest() (SonarCloud python:S3776) -- see
    _add_search_suggestion.
    """
    for book in get_liturgy_books(max_items=120):
        title = book.get("title", "")
        if title and q_lower in title.lower():
            _add_search_suggestion(suggestions, seen, "prayer", title, title, "Sefaria liturgy", 85)


def _collect_text_hit_suggestions(query, size, metadata_filters, suggestions, seen, search_library):
    """Split out of search_suggest() (SonarCloud python:S3776) -- see
    _add_search_suggestion.
    """
    for hit in search_library(query, size=size, metadata_filters=metadata_filters):
        ref = hit.get("ref", "")
        he_ref = (hit.get("heRef", "") or "").strip()
        categories = " > ".join(hit.get("categories", [])[:3])
        if ref:
            _add_search_suggestion(
                suggestions, seen, "text", ref, ref,
                categories or "Sefaria text", 70, label_he=he_ref or ref,
            )


@routes_library.route("/api/search/suggest")
def search_suggest():
    """Omnibox suggestions: texts, prayers, communities, and AI query option."""
    from backend.sefaria_library import search_library, get_liturgy_books

    query = (request.args.get("q", "") or "").strip()
    size = _coerce_int(request.args.get("size"), 8, min_value=1, max_value=20)
    metadata_filters = _extract_search_metadata_filters()
    if not query:
        return jsonify([])

    q_lower = query.lower()
    suggestions = []
    seen = set()

    alias_ref = QUICK_TEXT_ALIASES.get(q_lower)
    if alias_ref:
        _add_search_suggestion(suggestions, seen, "text", alias_ref, alias_ref, "Popular Torah alias", 100)

    _collect_community_name_suggestions(q_lower, suggestions, seen)
    _collect_community_alias_suggestions(q_lower, suggestions, seen)
    _collect_prayer_suggestions(q_lower, suggestions, seen, get_liturgy_books)
    _collect_text_hit_suggestions(query, size, metadata_filters, suggestions, seen, search_library)

    _add_search_suggestion(suggestions, seen, "ask", f"Ask Sh'elah: {query}", query,
                            "AI synthesis", 40)

    suggestions.sort(key=lambda x: x.get("score", 0), reverse=True)
    return jsonify(suggestions[:size])


@routes_library.route("/api/text/<path:ref>/links")
def get_text_links(ref):
    """Returns all linked commentaries & parallel texts for a given ref."""
    from backend.sefaria_library import get_linked_texts
    decoded_ref = _decode_route_ref(ref)
    return jsonify(get_linked_texts(decoded_ref))


@routes_library.route("/api/text/<path:ref>/graph")
def get_text_graph(ref):
    """Build a lightweight source graph around a text reference."""
    from backend.sefaria_library import get_linked_texts

    decoded_ref = _decode_route_ref(ref)
    links = get_linked_texts(decoded_ref)
    nodes = [{"id": decoded_ref, "label": decoded_ref, "kind": "root"}]
    edges = []
    seen = {decoded_ref}

    for category, items in (links or {}).items():
        for item in (items or [])[:14]:
            target = (item or {}).get("ref", "")
            if not target:
                continue
            if target not in seen:
                seen.add(target)
                nodes.append({
                    "id": target,
                    "label": target,
                    "kind": "linked",
                    "category": category,
                })
            edges.append({
                "source": decoded_ref,
                "target": target,
                "label": category,
            })

    return jsonify({
        "ref": ref,
        "nodes": nodes,
        "edges": edges,
    })


@routes_library.route("/api/library/category/<path:category>")
def library_category(category):
    """Returns all books in a given Sefaria category."""
    from backend.sefaria_library import get_category_contents
    return jsonify(get_category_contents(category))


# ─── TEXTS INDEX (for top menu) ───────────────────────────────────────────────
@routes_library.route("/api/texts-index")
def get_texts_index():
    """Returns complete index of browsable texts: prayers, communities, Sefaria."""
    from backend.sefaria_library import get_liturgy_books

    return jsonify({
        "siddur": {
            "title": "Sefaria Prayer Books",
            "items": [b.get("title") for b in get_liturgy_books(max_items=200)]
        },
        "merkava": {
            "title": "Community Customs (Merkava)",
            "items": list(COMMUNITIES.keys())
        },
        "sefaria": {
            "title": "Jewish Text Library",
            "items": ["Tanakh", "Mishnah", "Talmud", "Halakhah", "Kabbalah"]
        }
    })
