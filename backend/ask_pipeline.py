"""
Transport-agnostic async ask pipeline for Sh'elah (NOT YET ADOPTED).

Intent: both the Flask /ask route (app.py) and the FastAPI /ask endpoint
(asgi.py) should eventually share this single orchestration implementation,
replacing the logic that is currently duplicated independently in each of
them. `run_ask_pipeline()` is meant to become that single source of truth.

Current status: this module is NOT imported or called by app.py or asgi.py.
Neither file references `ask_pipeline` or `run_ask_pipeline` anywhere — each
maintains its own separate, independently-evolving /ask implementation today.
This is a staging/reference implementation only.

Unverified: `run_ask_pipeline()` takes a `flask_app_module` argument and
expects attributes such as `_store_user_memory_summary`,
`_build_ask_tool_context`, and `_compact_ai_sources` to be present on it.
Whether that still matches the current shape of app.py / backend/rag.py /
backend/helpers.py has not been confirmed end-to-end, and no test exercises
this function (0% coverage). Do not treat this module as drop-in-safe or
wire it into app.py/asgi.py without a dedicated correctness review and test
pass first.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers shared with asgi.py
# ---------------------------------------------------------------------------

def _flatten_sources_for_ai(
    primary_sources: list[dict[str, Any]],
    answer_language: str = "en",
) -> list[dict[str, str]]:
    """Collapse structured source dicts into flat {ref, text} pairs for the AI."""
    flattened = []
    use_hebrew = str(answer_language or "").strip().lower() == "he"
    for src in primary_sources:
        if not isinstance(src, dict):
            continue
        lines_raw = src.get("lines")
        lines = lines_raw if isinstance(lines_raw, list) else []
        en_lines = [
            str(
                (line.get("he") or line.get("en")) if use_hebrew
                else (line.get("en") or line.get("he")) or ""
            ).strip()
            for line in lines
            if isinstance(line, dict)
        ]
        text = " ".join(line for line in en_lines if line)
        ref = str(src.get("ref") or "").strip()
        if not ref and not text:
            continue
        flattened.append({"ref": ref, "text": text})
    return flattened


def _safe_list(value: Any) -> list:
    return value if isinstance(value, list) else []


# ---------------------------------------------------------------------------
# AskPipelineResult
# ---------------------------------------------------------------------------

class AskPipelineResult:
    """Structured result from run_ask_pipeline()."""

    __slots__ = (
        "answer",
        "confidence",
        "sources",
        "wiki",
        "customs",
        "meta",
        "is_fallback",
        "is_strict_blocked",
        "is_security_blocked",
        "structured",
        "fallback_detail",
    )

    def __init__(self, **kwargs: Any) -> None:
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot))


# ---------------------------------------------------------------------------
# Main pipeline entry point
# ---------------------------------------------------------------------------

async def run_ask_pipeline(
    *,
    question: str,
    mode: str,
    canonical_lens: str,
    answer_language: str,
    user_id: str | None,
    question_was_sanitized: bool,
    # Injected collaborators (imported by callers to avoid circular imports)
    claude_module: Any,
    sefaria_module: Any,
    search_module: Any,
    flask_app_module: Any,
) -> AskPipelineResult:
    """
    Orchestrate a full /ask request asynchronously.

    All blocking I/O is wrapped in ``asyncio.to_thread`` so this coroutine is
    safe to await from the FastAPI event loop or from a dedicated thread pool.

    Parameters
    ----------
    question:
        Sanitized user question.
    mode:
        Answer mode (balanced / practical / sources / strict).
    canonical_lens:
        Resolved community lens string.
    answer_language:
        "en" or "he".
    user_id:
        Clerk user ID if authenticated, else None.
    question_was_sanitized:
        Whether the question was modified by the sanitiser.
    claude_module / sefaria_module / search_module / flask_app_module:
        Module references passed in by the caller to avoid circular imports
        at module load time.
    """

    # ── 0. Prayer early-return ──────────────────────────────────────────────
    prayer_keywords = ["Shacharit", "Mincha", "Maariv", "Kiddush", "Havdalah"]
    if any(kw in question for kw in prayer_keywords):
        flask_app_module.DEVTOOLS_STATS["answers_total"] += 1
        prayer_answer = (
            f"Prayer Service Guide\n\n{question}\n\n"
            "You can browse full liturgy books and services from the prayer sections. "
            "For practical application, compare local community custom with your rabbi's guidance."
        )
        if answer_language == "he":
            prayer_answer = (
                f"מדריך תפילה\n\n{question}\n\n"
                "ניתן לעיין בספרי התפילה והשירותים הליטורגיים המלאים באזור התפילה. "
                "להכרעה מעשית יש להשוות למנהג הקהילה המקומית ולהתייעץ עם הרב שלך."
            )
        return AskPipelineResult(
            answer=prayer_answer,
            confidence=0.85,
            sources=[{
                "ref": "Sefaria Liturgy",
                "title": "Sefaria Prayer Books",
                "lines": [{"en": f"Prayer Service: {question}", "he": f"תפילה: {question}"}],
            }],
            wiki=[],
            customs=[],
            meta={
                "mode": mode,
                "language": answer_language,
                "community_lens": canonical_lens,
                "source_count": 1,
                "custom_count": 0,
                "generated_at": int(time.time()),
                "fallback": False,
            },
            is_fallback=False,
        )

    # ── 1. Parallel source / knowledge collection ───────────────────────────
    primary_refs_task = asyncio.create_task(
        asyncio.to_thread(sefaria_module.find_refs_for_question, question)
    )
    halachipedia_task = asyncio.create_task(
        search_module.async_search_halachipedia(question)
    )
    wiki_task = asyncio.create_task(search_module.async_search_wikipedia(question))
    knowledge_task = asyncio.create_task(
        asyncio.to_thread(
            flask_app_module._retrieve_community_knowledge,
            question,
            canonical_lens,
            flask_app_module.RAG_TOP_KNOWLEDGE_ROWS,
        )
    )
    memory_task = asyncio.create_task(
        asyncio.to_thread(
            flask_app_module._fetch_user_memory_summaries,
            user_id,
            flask_app_module.RAG_MEMORY_ROWS,
        )
    )

    primary_refs_raw, halachipedia_info, wiki_info, knowledge_rows, user_memory_summaries = (
        await asyncio.gather(
            primary_refs_task, halachipedia_task, wiki_task, knowledge_task, memory_task,
        )
    )

    primary_refs = _safe_list(primary_refs_raw)[:4]

    # Load primary source texts concurrently.
    async def _load_source(ref: str) -> dict[str, Any] | None:
        from backend.data_service import ShelahEngine
        engine = ShelahEngine()
        try:
            result = await asyncio.to_thread(engine.get_library_text, ref)
            return result if isinstance(result, dict) else None
        except Exception as exc:
            logger.debug("Source load failed ref=%r: %s", ref, exc)
            return None

    primary_sources = [
        s for s in await asyncio.gather(*[_load_source(r) for r in primary_refs])
        if s is not None
    ]

    knowledge_rows = _safe_list(knowledge_rows)
    user_memory_summaries = _safe_list(user_memory_summaries)

    halachipedia_list = [halachipedia_info] if isinstance(halachipedia_info, dict) else []
    wiki_list = [wiki_info] if isinstance(wiki_info, dict) else []
    customs_info = flask_app_module._knowledge_rows_to_customs(knowledge_rows)
    flat_sources = _flatten_sources_for_ai(primary_sources, answer_language=answer_language)

    has_primary = bool(flat_sources)
    has_customs = bool(knowledge_rows)
    has_external = bool(halachipedia_list)
    use_tertiary_web = not has_primary and not has_customs and not has_external
    wiki_context = wiki_list if use_tertiary_web else []

    # ── 2. Strict-mode guard ────────────────────────────────────────────────
    if mode == "strict" and not flat_sources:
        flask_app_module.DEVTOOLS_STATS["answers_total"] += 1
        flask_app_module.DEVTOOLS_STATS["strict_blocks"] += 1
        flask_app_module.DEVTOOLS_STATS["fallback_answers"] += 1
        return AskPipelineResult(
            answer=(
                "Strict Sources Mode could not complete this request because no primary Sefaria sources "
                "were matched with sufficient confidence. Please refine the question with a text reference."
            ),
            confidence=0.2,
            sources=flask_app_module._compact_ai_sources(primary_sources),
            wiki=wiki_list + halachipedia_list,
            customs=customs_info,
            meta={
                "mode": mode,
                "community_lens": canonical_lens,
                "source_count": 0,
                "custom_count": len(customs_info),
                "generated_at": int(time.time()),
                "fallback": True,
                "strict_blocked": True,
            },
            is_fallback=True,
            is_strict_blocked=True,
        )

    # ── 3. AI synthesis ─────────────────────────────────────────────────────
    try:
        # Build tool context (blocking helper, run off the event loop).
        from backend.data_service import ShelahEngine as _E
        _engine = _E()
        tool_context = await asyncio.to_thread(
            flask_app_module._build_ask_tool_context, _engine
        )
        if not isinstance(tool_context, dict):
            tool_context = {}
        tool_context["async"] = True

        result = await claude_module.ask_ai_async(
            question=question,
            sefaria_sources=flat_sources,
            customs=customs_info,
            user_memories=user_memory_summaries,
            wiki=wiki_context,
            halachipedia=halachipedia_list,
            mode=mode,
            community_lens=canonical_lens,
            answer_language=answer_language,
            tool_context=tool_context,
        )

        result_error = str(result.get("error") or "")
        if result_error and not result_error.startswith("security_blocked"):
            raise RuntimeError(result_error or "AI request failed")

        structured = result.get("structured")
        if not isinstance(structured, dict):
            structured = None

        if structured:
            raw_answer = claude_module.render_structured_markdown(
                structured, answer_language=answer_language
            )
        else:
            raw_answer = str(result.get("answer") or "").strip()

        if not raw_answer:
            raise RuntimeError("AI response was empty")

        needs_web_warning = use_tertiary_web and bool(wiki_context)
        needs_internal = (
            not has_primary and not has_customs and not has_external and not wiki_context
        )
        attribution = flask_app_module._build_source_attribution_note(
            has_sefaria=has_primary,
            has_customs=has_customs,
            has_whitelisted_external=has_external,
            has_general_web=bool(wiki_context),
            has_internal_knowledge=needs_internal,
        )
        normalized = flask_app_module._compose_answer_with_prefixes(
            raw_answer,
            include_web_warning=needs_web_warning,
            source_attribution_note=attribution,
        )
        if not str(normalized or "").strip():
            raise RuntimeError("AI response normalized to empty content")

        await asyncio.to_thread(
            flask_app_module._store_user_memory_summary, user_id, question, normalized
        )
        flask_app_module.DEVTOOLS_STATS["answers_total"] += 1
        display_sources = flask_app_module._compact_ai_sources(primary_sources)

        ai_cited: list[str] = []
        if isinstance(structured, dict):
            for s in (structured.get("sources") or []):
                s_str = str(s or "").strip()
                if s_str:
                    ai_cited.append(s_str)

        return AskPipelineResult(
            answer=normalized,
            confidence=result.get("confidence"),
            sources=display_sources,
            wiki=wiki_list + halachipedia_list,
            customs=customs_info,
            structured=structured,
            meta={
                "mode": mode,
                "language": answer_language,
                "community_lens": canonical_lens,
                "source_count": len(primary_sources),
                "custom_count": len(customs_info),
                "knowledge_count": len(knowledge_rows),
                "memory_count": len(user_memory_summaries),
                "identity_aware": bool(user_id),
                "generated_at": int(time.time()),
                "fallback": bool(result.get("is_fallback", False)),
                "structured": bool(structured),
                "is_prohibited": bool((structured or {}).get("is_prohibited", False)),
                "input_sanitized": question_was_sanitized,
                "security": result.get("security") or {},
                "async": True,
            },
            is_fallback=False,
            is_security_blocked=result_error.startswith("security_blocked"),
        )

    except Exception as ai_error:
        logger.warning("AI synthesis failed: %s", ai_error, exc_info=True)
        await asyncio.to_thread(
            flask_app_module._capture_backend_error,
            "ask_pipeline_ai_error",
            ai_error,
            {"question": question, "mode": mode, "community_lens": canonical_lens},
        )

        # ── 4. Fallback: halakhic source discovery ──────────────────────────
        fallback_payload = await asyncio.to_thread(
            flask_app_module.get_halakhic_sources, question
        )
        fallback_warning = str(fallback_payload.get("warning") or "").strip()
        fallback_counts = fallback_payload.get("counts", {})
        fallback_level = str(fallback_payload.get("fallback_level") or "").strip().lower()
        fallback_note = flask_app_module._build_source_attribution_note(
            has_sefaria=bool(fallback_counts.get("sefaria") or fallback_counts.get("specific_api")),
            has_customs=bool(customs_info),
            has_whitelisted_external=bool(fallback_counts.get("external")),
            has_general_web=fallback_level == "web-last-resort",
            has_internal_knowledge=fallback_level == "internal-ai-knowledge",
        )
        fallback_answer = flask_app_module._compose_answer_with_prefixes(
            "## Ruling\n\nAI synthesis unavailable. Returning discovered halakhic references.",
            include_web_warning=bool(fallback_warning),
            source_attribution_note=fallback_note,
        )
        await asyncio.to_thread(
            flask_app_module._store_user_memory_summary, user_id, question, fallback_answer
        )
        flask_app_module.DEVTOOLS_STATS["answers_total"] += 1
        flask_app_module.DEVTOOLS_STATS["fallback_answers"] += 1

        return AskPipelineResult(
            answer=fallback_answer,
            confidence=0.4,
            sources=flask_app_module._compact_ai_sources(fallback_payload.get("sources", [])),
            wiki=wiki_list + halachipedia_list,
            customs=customs_info,
            meta={
                "mode": mode,
                "language": answer_language,
                "community_lens": canonical_lens,
                "source_count": fallback_payload.get("source_count", 0),
                "custom_count": len(customs_info),
                "knowledge_count": len(knowledge_rows),
                "memory_count": len(user_memory_summaries),
                "identity_aware": bool(user_id),
                "generated_at": int(time.time()),
                "fallback": True,
                "status": fallback_payload.get("status", "fallback"),
                "fallback_detail": {
                    "keywords": fallback_payload.get("keywords", []),
                    "sequence": fallback_payload.get("sequence", []),
                    "counts": fallback_payload.get("counts", {}),
                    "level": fallback_payload.get("fallback_level", "unknown"),
                    "warning": fallback_warning,
                    "reason": str(ai_error),
                },
                "async": True,
            },
            is_fallback=True,
            fallback_detail=fallback_payload,
        )
