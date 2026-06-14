"""
Retrieval-augmented-generation helpers for Sh'elah.

Extracted verbatim from ``app.py`` (Phase 1, Step 1 of the zero-breakage
backend split). This module owns ask-time tool context assembly, community
knowledge retrieval/scoring, and answer prefix composition.

Shared collaborators (Supabase client factory, keyword extraction, text
normalization, community detection, and related constants) remain defined
in ``app.py`` and are reached here via lazy ``import app as _app`` inside
each function body.

Why lazy imports?  ``app.py`` imports from this module (re-import shims),
so a module-level ``from app import ...`` here would create a circular
dependency whose safety depends entirely on load ordering — a fragile
invariant that breaks silently when anything in ``app.py`` is reordered.
Lazy imports resolve to the already-cached module object in ``sys.modules``
on every call after startup, so there is zero runtime cost difference.

``app.py`` re-imports the public symbols so existing consumers —
including the async path in ``asgi.py`` — keep working unchanged.
"""

import os
import re


def _compose_answer_with_prefixes(body_text, *, include_web_warning=False, source_attribution_note=""):
    body = str(body_text or "").strip()
    if not body:
        return ""

    blocks = []
    if include_web_warning:
        import app as _app  # lazy — app is fully loaded by the time this is called
        blocks.append(_app.WEB_LAST_RESORT_WARNING)

    attribution = str(source_attribution_note or "").strip()
    if attribution:
        blocks.append(attribution)

    if blocks:
        blocks.append(body)
        return "\n\n".join(blocks)

    return body


def _build_ask_tool_context(engine):
    import app as _app  # lazy — app is fully loaded by the time this is called
    context = {
        "route": "/ask",
        "auth_enforced": _app.CLERK_ENFORCE_AUTH,
        "trusted_source_priority": "Sefaria, HebrewBooks, Halachipedia, Peninei Halakha (YHB)",
        "factuality_guardrail": "Reject random/non-halakhic domains and avoid generic English Bible websites when validating web context.",
    }

    try:
        zmanim_payload = engine.get_zmanim("standard")
        if isinstance(zmanim_payload, dict):
            metadata = zmanim_payload.get("metadata", {})
            zmanim = zmanim_payload.get("zmanim", {})

            if isinstance(metadata, dict):
                context["civil_date"] = metadata.get("date")
                context["hebrew_date"] = metadata.get("hebrew_date")
                context["parasha"] = metadata.get("parasha")
                context["holiday"] = metadata.get("holiday")
                context["timezone"] = metadata.get("timezone")

            if isinstance(zmanim, dict):
                snapshot = {}
                for key in (
                    "Dawn (16.1° / 72m)",
                    "Sunrise",
                    "Latest Shema (GRA)",
                    "Plag HaMincha",
                    "Sunset",
                    "Nightfall (3 Stars)",
                ):
                    value = str(zmanim.get(key) or "").strip()
                    if value and value != "N/A":
                        snapshot[key] = value

                if snapshot:
                    context["zmanim_snapshot"] = snapshot
    except Exception:
        # Keep ask flow resilient even if zmanim context is unavailable.
        pass

    return context


def _score_community_knowledge_row(row, keywords, canonical_lens):
    topic = str(row.get("topic") or "").lower()
    source = str(row.get("halakhic_source") or "").lower()
    content = str(row.get("content") or "").lower()
    community_name = str(row.get("community_name") or "").lower()

    score = 0
    if canonical_lens and canonical_lens != "All":
        lens_text = canonical_lens.lower().strip()
        if lens_text and lens_text in community_name:
            score += 8

    for keyword in keywords:
        if keyword in topic:
            score += 8
        if keyword in source:
            score += 4
        if keyword in content:
            score += 2

    return score


def _community_filter_from_request(query, canonical_lens):
    if canonical_lens and canonical_lens != "All":
        return canonical_lens

    import app as _app  # lazy — app is fully loaded by the time this is called
    detected = _app._detect_community_in_text(query)
    return detected or None


def _build_knowledge_text_or_filter(keywords, max_keywords=6):
    conditions = []
    for keyword in (keywords or [])[:max_keywords]:
        clean = re.sub(r"[^A-Za-z0-9֐-׿\-]",
                       "", str(keyword or "").strip())
        if not clean:
            continue

        pattern = f"%{clean}%"
        conditions.extend([
            f"topic.ilike.{pattern}",
            f"content.ilike.{pattern}",
        ])

    return ",".join(conditions)


def _retrieve_community_knowledge(query, canonical_lens="All", max_rows=None):
    import app as _app  # lazy — app is fully loaded by the time this is called
    supabase = _app._get_supabase_client()
    if not supabase:
        return []

    target_rows = max_rows or _app.RAG_TOP_KNOWLEDGE_ROWS
    keywords = _app._extract_query_keywords(query, max_keywords=10)
    community_filter = _community_filter_from_request(query, canonical_lens)
    text_or_filter = _build_knowledge_text_or_filter(keywords)

    query_row_cap = max(50, min(600, target_rows * 25))

    try:
        def run_query(apply_text_filter=True):
            table = supabase.table(_app.SUPABASE_COMMUNITY_KNOWLEDGE_TABLE).select(
                "id,community_name,topic,halakhic_source,content"
            )

            if community_filter:
                # Case-insensitive match so Ashkenaz/Ashkenazi variants still return rows.
                table = table.ilike("community_name", f"%{community_filter}%")

            if apply_text_filter and text_or_filter:
                table = table.or_(text_or_filter)

            result = table.limit(query_row_cap).execute()
            return result.data if isinstance(result.data, list) else []

        rows = run_query(apply_text_filter=True)

        # Fallback to community-only retrieval when text filter is too restrictive.
        if not rows and community_filter and text_or_filter:
            rows = run_query(apply_text_filter=False)
    except Exception:
        return []

    ranked = []
    for row in rows:
        if not isinstance(row, dict):
            continue

        score = _score_community_knowledge_row(
            row,
            keywords,
            community_filter or canonical_lens,
        )
        if score <= 0 and keywords and text_or_filter:
            continue

        ranked.append((score, row))

    ranked.sort(
        key=lambda item: (
            item[0],
            len(str(item[1].get("topic") or "")),
        ),
        reverse=True,
    )

    top_rows = []
    for score, row in ranked[:target_rows]:
        top_rows.append({
            "id": str(row.get("id") or "").strip(),
            "community_name": str(row.get("community_name") or "").strip(),
            "topic": str(row.get("topic") or "").strip(),
            "halakhic_source": str(row.get("halakhic_source") or "").strip(),
            "content": _app._normalize_rag_text(row.get("content")),
            "score": score,
        })

    return top_rows


# ── RAG constants ─────────────────────────────────────────────────────────────

def _env_int(name, default):
    raw_value = os.environ.get(name)
    if not raw_value:
        return default
    try:
        if isinstance(default, int) and not isinstance(default, bool):
            return int(default)
        return int(raw_value)
    except Exception:
        return default


RAG_TOP_KNOWLEDGE_ROWS = _env_int("RAG_TOP_KNOWLEDGE_ROWS", 5)
RAG_MEMORY_ROWS = _env_int("RAG_MEMORY_ROWS", 2)


# ── Knowledge-row helpers ─────────────────────────────────────────────────────


def _knowledge_rows_to_customs(rows):
    customs = []
    for row in rows:
        if not isinstance(row, dict):
            continue

        topic = str(row.get("topic") or "").strip()
        source = str(row.get("halakhic_source") or "").strip()
        content = str(row.get("content") or "").strip()
        customs.append({
            "community": str(row.get("community_name") or "").strip(),
            "topic": topic,
            "ruling": content,
            "source": source,
            "notes": f"Topic: {topic}" if topic else "",
        })

    return customs


# ── User memory helpers ───────────────────────────────────────────────────────


def _fetch_user_memory_summaries(user_id, limit=None):
    import app as _app  # lazy — avoids circular import at module load time
    if not user_id:
        return []

    supabase = _app._get_user_scoped_supabase_client()
    if not supabase and not _app.STRICT_SUPABASE_RLS:
        supabase = _app._get_supabase_client()
    if not supabase:
        return []

    target_limit = limit or RAG_MEMORY_ROWS
    try:
        result = (
            supabase
            .table(_app.SUPABASE_USER_MEMORIES_TABLE)
            .select("summary,created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(target_limit)
            .execute()
        )
    except Exception:
        return []

    rows = result.data if isinstance(result.data, list) else []
    summaries = []
    for row in rows:
        if not isinstance(row, dict):
            continue

        summary = _app._normalize_rag_text(row.get("summary"), max_chars=260)
        if not summary:
            continue

        summaries.append({
            "summary": summary,
            "created_at": row.get("created_at"),
        })

    return summaries


def _store_user_memory_summary(user_id, question, answer):
    import app as _app  # lazy — avoids circular import at module load time
    from uuid import uuid4

    if not user_id:
        return

    supabase = _app._get_user_scoped_supabase_client()
    if not supabase and not _app.STRICT_SUPABASE_RLS:
        supabase = _app._get_supabase_client()
    if not supabase:
        return

    summary = _app._build_interaction_summary(question, answer)
    if not summary:
        return

    payload = {
        "id": str(uuid4()),
        "user_id": user_id,
        "summary": summary,
    }

    try:
        supabase.table(_app.SUPABASE_USER_MEMORIES_TABLE).insert(payload).execute()
    except Exception:
        # Memory write failures should never block the user response path.
        return
