"""ASGI entrypoint for incremental async migration.

This module keeps the existing Flask app intact while exposing an async `/ask`
endpoint implemented with FastAPI + httpx-enabled AI/search calls.
All other routes are served by the mounted Flask WSGI app.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from typing import Any

# OrderedDict gives O(1) move_to_end for LRU eviction.
_RateLimitStore = collections.OrderedDict

logger = logging.getLogger(__name__)

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.wsgi import WSGIMiddleware
from pydantic import BaseModel, Field

import app as flask_app_module
from app import get_halakhic_sources
from backend import claude, search
from backend.auth import _verify_clerk_token, CLERK_ENFORCE_AUTH
from backend import sefaria as _backend_sefaria
from backend.data_service import ShelahEngine
from backend.rag import _build_ask_tool_context, _retrieve_community_knowledge, _compose_answer_with_prefixes
from backend.rag import _knowledge_rows_to_customs, RAG_TOP_KNOWLEDGE_ROWS, RAG_MEMORY_ROWS
from backend.rag import _fetch_user_memory_summaries, _store_user_memory_summary
from backend.helpers import _sanitize_answer_mode, _compact_ai_sources
from backend.helpers import _build_source_attribution_note, _canonicalize_community_name
from backend.logging_setup import _capture_backend_error

# ─── Simple in-process rate limiter for FastAPI /ask ──────────────────────────
# Each IP is tracked with a deque of request timestamps. This avoids adding
# slowapi/Redis just for a single endpoint and matches the Flask limit (20/min).
_RATE_LIMIT_WINDOW_SECONDS = 60
_RATE_LIMIT_MAX_REQUESTS = 20
# OrderedDict gives O(1) LRU eviction via move_to_end — evicts least-recently-used
# IP instead of oldest-inserted, preventing an attacker cycling 2 048 IPs from
# flushing active users' counters.
_rate_limit_store: _RateLimitStore = _RateLimitStore()
_RATE_LIMIT_STORE_MAX_KEYS = 2048  # cap memory


def _get_client_ip(request: Request) -> str:
    """Extract client IP from Cloudflare or standard proxy headers."""
    for header in ("CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP"):
        val = request.headers.get(header, "").split(",")[0].strip()
        if val:
            return val
    return request.client.host if request.client else "unknown"


def _check_rate_limit(ip: str) -> bool:
    """Return True if the request is allowed, False if rate-limited.

    Uses an OrderedDict for LRU eviction: each access moves the key to the end
    so the least-recently-used entry is always at the front for eviction.
    """
    now = time.monotonic()
    if ip not in _rate_limit_store:
        # Evict LRU key (front of OrderedDict) if store is at capacity.
        if len(_rate_limit_store) >= _RATE_LIMIT_STORE_MAX_KEYS:
            _rate_limit_store.popitem(last=False)
        _rate_limit_store[ip] = collections.deque()
    else:
        # Move to end to mark as most-recently-used.
        _rate_limit_store.move_to_end(ip)

    timestamps = _rate_limit_store[ip]
    cutoff = now - _RATE_LIMIT_WINDOW_SECONDS
    while timestamps and timestamps[0] < cutoff:
        timestamps.popleft()
    if len(timestamps) >= _RATE_LIMIT_MAX_REQUESTS:
        return False
    timestamps.append(now)
    return True


class AskRequest(BaseModel):
    question: str = Field(default="")
    mode: str | None = None
    community: str | None = None
    language: str | None = None


def _extract_user_id_from_bearer(authorization: str | None) -> str | None:
    header = str(authorization or "").strip()
    if not header:
        return None

    if not header.lower().startswith("bearer "):
        return None

    token = header.split(" ", 1)[1].strip()
    if not token:
        return None

    try:
        claims = _verify_clerk_token(token)
    except Exception as exc:
        logger.debug("Bearer token verification failed: %s", exc)
        return None

    user_id = str(claims.get("sub") or "").strip()
    return user_id or None


def _flatten_sources_for_ai(primary_sources: list[dict[str, Any]], answer_language: str = "en") -> list[dict[str, str]]:
    flattened = []
    use_hebrew = str(answer_language or "").strip().lower() == "he"
    for src in primary_sources:
        if not isinstance(src, dict):
            continue
        lines_raw = src.get("lines")
        lines = lines_raw if isinstance(lines_raw, list) else []
        en_lines = [
            str((line.get("he") or line.get("en")) if use_hebrew else (
                line.get("en") or line.get("he")) or "").strip()
            for line in lines
            if isinstance(line, dict)
        ]
        text = " ".join([line for line in en_lines if line])
        ref = str(src.get("ref") or "").strip()
        if not ref and not text:
            continue
        flattened.append({"ref": ref, "text": text})
    return flattened


def _safe_json_payload(value: Any, default: Any) -> Any:
    return value if isinstance(value, type(default)) else default


async def _collect_primary_sources(question: str) -> tuple[list[str], list[dict[str, Any]]]:
    primary_refs = await asyncio.to_thread(
        _backend_sefaria.find_refs_for_question,
        question,
    )
    refs = primary_refs if isinstance(primary_refs, list) else []

    async def _load_one(ref: str) -> dict[str, Any] | None:
        engine = ShelahEngine()
        try:
            source = await asyncio.to_thread(engine.get_library_text, ref)
            return source if isinstance(source, dict) else None
        except Exception as exc:
            logger.debug("Source load failed for ref=%r: %s", ref, exc)
            return None

    results = await asyncio.gather(*[_load_one(ref) for ref in refs])
    primary_sources = [s for s in results if s is not None]
    return refs, primary_sources


async def _build_tool_context() -> dict[str, Any]:
    def _build() -> dict[str, Any]:
        engine = ShelahEngine()
        return _build_ask_tool_context(engine)

    try:
        payload = await asyncio.to_thread(_build)
        return payload if isinstance(payload, dict) else {"route": "/ask", "async": True}
    except Exception as exc:
        logger.debug("Tool context build failed: %s", exc)
        return {"route": "/ask", "async": True}


fastapi_app = FastAPI(title="Shelah ASGI", version="1.0.0")


@fastapi_app.get("/api/async/health")
async def async_health() -> dict[str, Any]:
    return {
        "ok": True,
        "runtime": "fastapi",
        "flask_mounted": True,
        "ts": int(time.time()),
    }


@fastapi_app.post("/ask")
async def ask_async(request: Request, payload: AskRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    client_ip = _get_client_ip(request)
    if not _check_rate_limit(client_ip):
        raise HTTPException(
            status_code=429, detail="Rate limit exceeded. Please wait before sending another request.")

    question = claude.sanitize_user_query(payload.question)
    question_was_sanitized = question != str(payload.question or "").strip()
    if not question:
        raise HTTPException(
            status_code=400, detail="No valid question provided")

    if CLERK_ENFORCE_AUTH:
        user_id = _extract_user_id_from_bearer(authorization)
        if not user_id:
            raise HTTPException(
                status_code=401, detail="Authentication required")
    else:
        user_id = _extract_user_id_from_bearer(authorization)

    mode = _sanitize_answer_mode(payload.mode)
    community_lens = str(payload.community or "All").strip() or "All"
    answer_language = str(payload.language or "en").strip().lower()
    if answer_language not in {"en", "he"}:
        answer_language = "en"
    canonical_lens = (
        "All"
        if community_lens.lower() == "all"
        else (_canonicalize_community_name(community_lens) or community_lens)
    )

    try:
        if any(prayer in question for prayer in ["Shacharit", "Mincha", "Maariv", "Kiddush", "Havdalah"]):
            flask_app_module.DEVTOOLS_STATS["answers_total"] += 1
            return {
                "answer": (
                    "Prayer Service Guide\n\n"
                    f"{question}\n\n"
                    "You can browse full liturgy books and services from the prayer sections. "
                    "For practical application, compare local community custom with your rabbi's guidance."
                ),
                "confidence": 0.85,
                "sources": [
                    {
                        "ref": "Sefaria Liturgy",
                        "title": "Sefaria Prayer Books",
                        "lines": [{"en": f"Prayer Service: {question}", "he": ""}],
                    }
                ],
                "customs": [],
                "meta": {
                    "mode": mode,
                    "community_lens": canonical_lens,
                    "source_count": 1,
                    "custom_count": 0,
                    "generated_at": int(time.time()),
                    "fallback": False,
                    "async": True,
                },
            }

        primary_task = asyncio.create_task(_collect_primary_sources(question))
        halachipedia_task = asyncio.create_task(
            search.async_search_halachipedia(question))
        wiki_task = asyncio.create_task(
            search.async_search_wikipedia(question))
        knowledge_task = asyncio.create_task(
            asyncio.to_thread(
                _retrieve_community_knowledge,
                question,
                canonical_lens,
                RAG_TOP_KNOWLEDGE_ROWS,
            )
        )
        memory_task = asyncio.create_task(
            asyncio.to_thread(
                _fetch_user_memory_summaries,
                user_id,
                RAG_MEMORY_ROWS,
            )
        )
        tool_context_task = asyncio.create_task(_build_tool_context())

        (_, primary_sources), halachipedia_info, wiki_info, knowledge_rows, user_memory_summaries, tool_context = await asyncio.gather(
            primary_task,
            halachipedia_task,
            wiki_task,
            knowledge_task,
            memory_task,
            tool_context_task,
        )

        primary_sources = _safe_json_payload(primary_sources, [])
        knowledge_rows = _safe_json_payload(knowledge_rows, [])
        user_memory_summaries = _safe_json_payload(user_memory_summaries, [])

        halachipedia_list = [halachipedia_info] if isinstance(
            halachipedia_info, dict) else []
        wiki_list = [wiki_info] if isinstance(wiki_info, dict) else []

        customs_info = _knowledge_rows_to_customs(
            knowledge_rows)
        flat_sources_for_ai = _flatten_sources_for_ai(
            primary_sources, answer_language=answer_language)

        has_primary_sources = bool(flat_sources_for_ai)
        has_customs = bool(knowledge_rows)
        has_whitelisted_external = bool(halachipedia_list)
        use_tertiary_web_context = (
            not has_primary_sources and not has_customs and not has_whitelisted_external
        )
        wiki_context_for_ai = wiki_list if use_tertiary_web_context else []

        if mode == "strict" and not flat_sources_for_ai:
            flask_app_module.DEVTOOLS_STATS["answers_total"] += 1
            flask_app_module.DEVTOOLS_STATS["strict_blocks"] += 1
            flask_app_module.DEVTOOLS_STATS["fallback_answers"] += 1
            display_sources = _compact_ai_sources(
                primary_sources)
            return {
                "answer": (
                    "Strict Sources Mode could not complete this request because no primary Sefaria sources "
                    "were matched with sufficient confidence. Please refine the question with a text reference."
                ),
                "confidence": 0.2,
                "wiki": wiki_list + halachipedia_list,
                "customs": customs_info,
                "sources": display_sources,
                "meta": {
                    "mode": mode,
                    "community_lens": canonical_lens,
                    "source_count": 0,
                    "custom_count": len(customs_info),
                    "generated_at": int(time.time()),
                    "fallback": True,
                    "strict_blocked": True,
                    "async": True,
                },
            }

        try:
            tool_context = tool_context if isinstance(tool_context, dict) else {
                "route": "/ask", "async": True}
            tool_context["async"] = True

            result = await claude.ask_ai_async(
                question=question,
                sefaria_sources=flat_sources_for_ai,
                customs=customs_info,
                user_memories=user_memory_summaries,
                wiki=wiki_context_for_ai,
                halachipedia=halachipedia_list,
                mode=mode,
                community_lens=canonical_lens,
                answer_language=answer_language,
                tool_context=tool_context,
            )

            result_error = str(result.get("error") or "")
            if result_error and not result_error.startswith("security_blocked"):
                raise RuntimeError(result_error or "AI request failed")

            structured_payload = result.get("structured")
            if not isinstance(structured_payload, dict):
                structured_payload = None

            raw_ai_answer = ""
            if structured_payload:
                raw_ai_answer = claude.render_structured_markdown(
                    structured_payload,
                    answer_language=answer_language,
                )
            else:
                raw_ai_answer = str(result.get("answer") or "").strip()

            if not raw_ai_answer:
                raise RuntimeError("AI response was empty")

            needs_web_warning = use_tertiary_web_context and bool(
                wiki_context_for_ai)
            needs_internal_knowledge = (
                not has_primary_sources
                and not has_customs
                and not has_whitelisted_external
                and not wiki_context_for_ai
            )

            source_attribution_note = _build_source_attribution_note(
                has_sefaria=has_primary_sources,
                has_customs=has_customs,
                has_whitelisted_external=has_whitelisted_external,
                has_general_web=bool(wiki_context_for_ai),
                has_internal_knowledge=needs_internal_knowledge,
            )
            normalized_answer = _compose_answer_with_prefixes(
                raw_ai_answer,
                include_web_warning=needs_web_warning,
                source_attribution_note=source_attribution_note,
            )
            if not str(normalized_answer or "").strip():
                raise RuntimeError("AI response normalized to empty content")

            result["answer"] = normalized_answer
            await asyncio.to_thread(
                _store_user_memory_summary,
                user_id,
                question,
                normalized_answer,
            )

            flask_app_module.DEVTOOLS_STATS["answers_total"] += 1
            display_sources = _compact_ai_sources(
                primary_sources)

            return {
                "answer": normalized_answer,
                "confidence": result.get("confidence"),
                "wiki": wiki_list + halachipedia_list,
                "customs": customs_info,
                "sources": display_sources,
                "meta": {
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
                    "structured": bool(structured_payload),
                    "is_prohibited": bool((structured_payload or {}).get("is_prohibited", False)),
                    "input_sanitized": question_was_sanitized,
                    "security": result.get("security") or {},
                    "async": True,
                },
            }

        except Exception as ai_error:
            await asyncio.to_thread(
                _capture_backend_error,
                "ask_ai_synthesis_failed_async",
                ai_error,
                {
                    "question": question,
                    "mode": mode,
                    "community_lens": canonical_lens,
                    "user_id": user_id or "",
                },
            )

            fallback_payload = await asyncio.to_thread(
                get_halakhic_sources,
                question,
            )
            fallback_warning = str(
                fallback_payload.get("warning") or "").strip()

            fallback_counts = fallback_payload.get("counts", {})
            fallback_level = str(fallback_payload.get(
                "fallback_level") or "").strip().lower()
            fallback_source_note = _build_source_attribution_note(
                has_sefaria=bool(fallback_counts.get("sefaria")
                                 or fallback_counts.get("specific_api")),
                has_customs=bool(customs_info),
                has_whitelisted_external=bool(fallback_counts.get("external")),
                has_general_web=fallback_level == "web-last-resort",
                has_internal_knowledge=fallback_level == "internal-ai-knowledge",
            )
            fallback_answer = _compose_answer_with_prefixes(
                "## Ruling\n\nAI synthesis unavailable. Returning discovered halakhic references.",
                include_web_warning=bool(fallback_warning),
                source_attribution_note=fallback_source_note,
            )

            await asyncio.to_thread(
                _store_user_memory_summary,
                user_id,
                question,
                fallback_answer,
            )

            flask_app_module.DEVTOOLS_STATS["answers_total"] += 1
            flask_app_module.DEVTOOLS_STATS["fallback_answers"] += 1
            fallback_sources = _compact_ai_sources(
                fallback_payload.get("sources", []))

            return {
                "answer": fallback_answer,
                "confidence": 0.4,
                "wiki": wiki_list + halachipedia_list,
                "customs": customs_info,
                "sources": fallback_sources,
                "meta": {
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
            }

    except HTTPException:
        raise
    except Exception as e:
        await asyncio.to_thread(
            _capture_backend_error,
            "ask_route_critical_error_async",
            e,
            {
                "question": question if "question" in locals() else "",
                "mode": mode if "mode" in locals() else "",
                "community_lens": canonical_lens if "canonical_lens" in locals() else "",
            },
        )
        raise HTTPException(
            status_code=500, detail=f"An internal error occurred while processing your request: {e}")


# Mount existing Flask app so all legacy routes continue to work.
fastapi_app.mount("/", WSGIMiddleware(flask_app_module.app))

# Export canonical ASGI application.
app = fastapi_app
