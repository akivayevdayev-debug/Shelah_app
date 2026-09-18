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

from backend.logging_setup import _capture_backend_error, hash_user_id
from backend.health_check import health


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


def _zmanim_snapshot_for_tool_context(zmanim):
    """The small subset of zmanim values worth surfacing in the AI tool
    context. Split out of _build_ask_tool_context() to keep this loop out of
    that function's own complexity count (SonarCloud python:S3776).
    """
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
    return snapshot


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
                snapshot = _zmanim_snapshot_for_tool_context(zmanim)
                if snapshot:
                    context["zmanim_snapshot"] = snapshot
    except Exception:
        # Keep ask flow resilient even if zmanim context is unavailable.
        pass

    return context


def _keyword_match_score(keywords, topic, source, content):
    """Sum of per-keyword topic/source/content hits. Split out of
    _score_community_knowledge_row() to keep this loop out of that
    function's own complexity count (SonarCloud python:S3776).
    """
    score = 0
    for keyword in keywords:
        if keyword in topic:
            score += 8
        if keyword in source:
            score += 4
        if keyword in content:
            score += 2
    return score


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

    return score + _keyword_match_score(keywords, topic, source, content)


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


def _run_community_knowledge_query(
    supabase, table_name, community_filter, text_or_filter, query_row_cap, apply_text_filter=True,
):
    """Runs the Supabase community_knowledge select with optional
    community-name/text-search filters. Moved to module level (out of
    _retrieve_community_knowledge()) since a nested closure's own branches
    count against the enclosing function's complexity, but a top-level
    function's don't (SonarCloud python:S3776).
    """
    table = supabase.table(table_name).select(
        "id,community_name,topic,halakhic_source,content"
    )

    if community_filter:
        # Case-insensitive match so Ashkenaz/Ashkenazi variants still return rows.
        table = table.ilike("community_name", f"%{community_filter}%")

    if apply_text_filter and text_or_filter:
        table = table.or_(text_or_filter)

    result = table.limit(query_row_cap).execute()
    return result.data if isinstance(result.data, list) else []


def _rank_community_knowledge_rows(rows, keywords, community_filter, canonical_lens):
    """Score raw community-knowledge rows and drop keyword-irrelevant ones.
    Split out of _retrieve_community_knowledge() to keep this loop out of
    that function's own complexity count (SonarCloud python:S3776).
    """
    ranked = []
    for row in rows:
        if not isinstance(row, dict):
            continue

        score = _score_community_knowledge_row(
            row,
            keywords,
            community_filter or canonical_lens,
        )

        # Always require keyword relevance — community-only matches (no topic/content hit)
        # would score exactly 8 from the community bonus but have zero keyword_score.
        community_name_lower = str(row.get("community_name") or "").lower()
        has_community_bonus = bool(
            community_filter
            and community_filter.lower().strip() in community_name_lower
        )
        keyword_score = score - (8 if has_community_bonus else 0)
        if keyword_score <= 0:
            continue  # off-topic for this question; skip regardless of community match

        ranked.append((score, row))
    return ranked


def _retrieve_community_knowledge(query, canonical_lens="All", max_rows=None):
    import app as _app  # lazy — app is fully loaded by the time this is called
    supabase = _app._get_supabase_client()
    if not supabase:
        return []

    if not health.is_healthy("community_knowledge"):
        return []

    target_rows = max_rows or _app.RAG_TOP_KNOWLEDGE_ROWS
    keywords = _app._extract_query_keywords(query, max_keywords=10)
    community_filter = _community_filter_from_request(query, canonical_lens)
    text_or_filter = _build_knowledge_text_or_filter(keywords)

    query_row_cap = max(50, min(600, target_rows * 25))

    try:
        rows = _run_community_knowledge_query(
            supabase, _app.SUPABASE_COMMUNITY_KNOWLEDGE_TABLE,
            community_filter, text_or_filter, query_row_cap, apply_text_filter=True,
        )
    except Exception as e:
        health.record_failure("community_knowledge")
        _capture_backend_error("community_knowledge_query_failed", e, {})
        return []
    health.record_success("community_knowledge")

    ranked = _rank_community_knowledge_rows(rows, keywords, community_filter, canonical_lens)

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
        return int(raw_value)
    except Exception:
        return default


# Kept as a literal (not env-sourced) to match app.py's own copy of these
# constants -- see app.py's RAG_TOP_KNOWLEDGE_ROWS/RAG_MEMORY_ROWS comment.
RAG_TOP_KNOWLEDGE_ROWS = 5
RAG_MEMORY_ROWS = 2


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


def _fetch_user_memory_summaries(user_id, limit=None, bearer_token=None):
    import app as _app  # lazy — avoids circular import at module load time
    if not user_id:
        return []

    supabase = _app._get_user_scoped_supabase_client(bearer_token)
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


def _store_ask_history(
    user_id,
    question,
    answer,
    *,
    sources=None,
    ai_cited_sources=None,
    community="All",
    mode="balanced",
    language="en",
    safety_class="ok",
    prompt_version=None,
):
    """Persist a completed ask interaction to the per-user ask_history table.

    safety_class/prompt_version are defensibility-logging metadata (plan.md
    §8.B.6): they let a stored answer's §8.B-AGE safety-routing outcome and
    governing system-prompt version be reconstructed during a dispute,
    without retaining the full prompt text itself. Requires
    scripts/migrate_ask_history_safety_metadata.sql to have been applied —
    see that file.
    """
    import app as _app
    from uuid import uuid4

    if not user_id:
        return

    supabase = _app._get_supabase_client()
    if not supabase:
        return

    payload = {
        "id": str(uuid4()),
        "user_id": user_id,
        "question": str(question or "")[:2000],
        "answer": str(answer or "")[:10000],
        "sources": sources if isinstance(sources, list) else [],
        "ai_cited_sources": ai_cited_sources if isinstance(ai_cited_sources, list) else [],
        "community": str(community or "All")[:100],
        "mode": str(mode or "balanced")[:50],
        "language": str(language or "en")[:10],
        "safety_class": str(safety_class or "ok")[:50],
        "prompt_version": str(prompt_version or "")[:50] or None,
    }

    try:
        supabase.table(_app.SUPABASE_ASK_HISTORY_TABLE).insert(payload).execute()
    except Exception as e:
        # plan.md §23.2.4: a PostgREST schema error must never be
        # indistinguishable from success — this is the exact defensibility-
        # logging table the accept_legal() clerk_id/user_id bug (§23.1) was
        # about, and safety_class/prompt_version are the columns
        # migrate_ask_history_safety_metadata.sql added. Keep the write
        # best-effort (never block the ask response), but make a failure
        # observable.
        _capture_backend_error("ask_history_store_failed", e, {"user_id_hash": hash_user_id(user_id)})
        return


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
    except Exception as e:
        # Memory write failures should never block the user response path
        # (kept non-fatal), but plan.md §23.2.4 requires the failure itself
        # stay observable rather than vanish into a bare `except: return`.
        _capture_backend_error("user_memory_store_failed", e, {"user_id_hash": hash_user_id(user_id)})
        return
