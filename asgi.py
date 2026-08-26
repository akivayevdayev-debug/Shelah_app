"""ASGI entrypoint for incremental async migration.

This module keeps the existing Flask app intact while exposing an async `/ask`
endpoint implemented with FastAPI + httpx-enabled AI/search calls.
All other routes are served by the mounted Flask WSGI app.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.wsgi import WSGIMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import app as flask_app_module
from backend import ask_pipeline, claude, search
from backend.auth import CLERK_ENFORCE_AUTH, extract_user_id_from_bearer_value
from backend.utils.search_provider import get_halakhic_sources
from backend import sefaria as _backend_sefaria
from backend.data_service import ShelahEngine
from backend.rag import _build_ask_tool_context, _retrieve_community_knowledge, _compose_answer_with_prefixes
from backend.rag import _knowledge_rows_to_customs, RAG_TOP_KNOWLEDGE_ROWS, RAG_MEMORY_ROWS
from backend.rag import _fetch_user_memory_summaries, _store_user_memory_summary, _store_ask_history
from backend.helpers import _sanitize_answer_mode, _compact_ai_sources, extract_ai_cited, _resolve_client_ip, _coarse_ai_error_reason
from backend.helpers import SECURITY_RESPONSE_HEADERS
from backend.helpers import _canonicalize_community_name
from backend.cache_policy import classify_cache_tier
from backend.cost_meter import check_user_budget_and_enforce, is_global_cost_breaker_tripped
from backend.rate_limit import RateLimitMiddleware
from backend import turnstile as _turnstile
# INIT-ORDERING (plan.md §17.4 — load-bearing, do not regress): this import
# is what triggers backend/logging_setup.py's module-level sentry_sdk.init()
# call (or confirms it already ran via an earlier import in this file, e.g.
# `import app as flask_app_module` above, which itself imports
# backend.logging_setup). Sentry requires init() to run before the app
# object is constructed — that's true here today, but only because this
# import happens to sit ~140 lines above `fastapi_app = FastAPI(...)`
# further down this file, not because anything enforces the ordering. If a
# future reshuffle moves FastAPI(...) construction earlier, or defers this
# import later, Sentry silently stops instrumenting the app — no exception,
# no log line. Keep this import (or an equivalent early import of
# backend.logging_setup) ahead of the FastAPI(...) call below.
from backend.logging_setup import _capture_backend_error, bind_client_key, bind_request_id, bind_user_id, get_logger, log_mitigation

logger = logging.getLogger(__name__)


def _get_client_ip(request: Request) -> str:
    """Extract client IP from trusted Vercel proxy headers (plan.md §16.1 D2:
    never CF-Connecting-IP -- this deployment has no Cloudflare in front of
    it, so that header is attacker-controlled)."""
    remote_addr = request.client.host if request.client else None
    return _resolve_client_ip(request.headers, remote_addr=remote_addr)


class AskRequest(BaseModel):
    question: str = Field(default="")
    mode: str | None = None
    community: str | None = None
    language: str | None = None
    # Cloudflare Turnstile token (plan.md §16.4 / §16.6 Phase 9c, backend/
    # turnstile.py). Only read/required once an anonymous caller has
    # crossed TURNSTILE_ANON_HOURLY_THRESHOLD and TURNSTILE_ENABLED=true;
    # ignored otherwise, so existing callers never need to send it.
    turnstile_token: str | None = None


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

# plan.md §16.3-L2 / §16.8.2: the single rate-limit enforcement point for
# both native FastAPI routes and every Flask route reached through the
# WSGIMiddleware mount below (see backend/rate_limit.py's module docstring
# for why one Starlette middleware registration covers both). Registered
# before request_id_middleware's own `add_middleware` call below so that
# middleware ends up OUTERMOST (Starlette's add_middleware prepends) --
# request_id_middleware still runs first, so a 429 from this middleware is
# logged with a request_id like every other response, while rate limiting
# still fully precedes route dispatch (FastAPI or Flask) either way.
fastapi_app.add_middleware(RateLimitMiddleware)

_request_logger = get_logger("shelah.request.asgi")

# plan.md §16.4 body cap, ASGI side: app.py's MAX_CONTENT_LENGTH only
# protects requests that reach Werkzeug/Flask through the WSGIMiddleware
# mount below -- it does nothing for this file's native FastAPI routes
# (same reason the rate limiter needed its own independent check here, see
# D1 in plan.md §16.1). Checked via the Content-Length header only (no body
# read) so an oversized request is rejected before any parsing work happens.
_MAX_ASGI_BODY_BYTES = 256 * 1024


@fastapi_app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Bind a request_id (contextvars) for every request through this ASGI app.

    asyncio.create_task()/asyncio.to_thread() copy the current context at
    creation time, so binding here — before ask_async() spawns its
    gather()'d tasks — is enough for the id to reach every task/thread that
    request spins up. The WSGI-mounted Flask app underneath gets its own
    independent bind_request_id() call (app.py's before_request); to keep
    both layers reporting the *same* id for a single client request, an
    id generated here is written back into the ASGI header list so Flask's
    header-based lookup finds it too.
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_ASGI_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"error": "Request body too large."},
                )
        except ValueError:
            pass

    incoming = request.headers.get("x-request-id")
    request_id = bind_request_id(incoming)

    if not incoming:
        raw_headers = [
            (k, v) for k, v in (request.scope.get("headers") or [])
            if k.lower() != b"x-request-id"
        ]
        raw_headers.append((b"x-request-id", request_id.encode("latin-1")))
        request.scope["headers"] = raw_headers

    start = time.monotonic()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        # Security headers (audit P2): this middleware wraps every request
        # through fastapi_app, including native routes like /ask that never
        # reach the WSGIMiddleware-mounted Flask app below and therefore
        # never see app.py's @app.after_request. setdefault-style (skip if
        # already present) so a WSGI-routed response that already carries
        # these from Flask's own after_request is left untouched.
        for header_name, header_value in SECURITY_RESPONSE_HEADERS.items():
            if header_name not in response.headers:
                response.headers[header_name] = header_value
        # plan.md §14.3.1: native routes (POST /ask, GET /api/async/health)
        # never reach app.py's Flask after_request hook, so without this they
        # shipped with NO Cache-Control header at all. setdefault-style, same
        # as the security headers above -- a WSGI-routed response that
        # already set its own Cache-Control via Flask's hook is untouched.
        if "cache-control" not in response.headers:
            cache_control = classify_cache_tier(request.method, request.url.path)
            if cache_control is not None:
                response.headers["Cache-Control"] = cache_control
        response.headers["X-Request-Id"] = request_id
        return response
    finally:
        _request_logger.info(
            "request_complete",
            extra={
                "http_method": request.method,
                "http_path": request.url.path,
                "http_status": status_code,
                "duration_ms": round((time.monotonic() - start) * 1000, 2),
            },
        )


@fastapi_app.get("/api/async/health")
async def async_health() -> dict[str, Any]:
    return {
        "ok": True,
        "runtime": "fastapi",
        "flask_mounted": True,
        "ts": int(time.time()),
    }


def _ask_async_prayer_result(question, mode, canonical_lens):
    """Stage 0 of ask_async(): prayer-keyword early return, or None if
    `question` isn't a prayer question. Split out to keep this pipeline
    stage's branching out of that route's own complexity count
    (SonarCloud python:S3776).
    """
    if not any(prayer in question for prayer in ["Shacharit", "Mincha", "Maariv", "Kiddush", "Havdalah"]):
        return None

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


async def _collect_ask_async_context(
    question, canonical_lens, user_id, answer_language, bearer_token=None,
):
    """Stage 1 of ask_async(): parallel source/knowledge/tool-context
    collection. Returns a context dict consumed by the strict-guard and
    AI-synthesis stages below. Split out of ask_async() (SonarCloud
    python:S3776) -- see _ask_async_prayer_result.

    bearer_token is the raw Authorization header value, threaded down to
    _fetch_user_memory_summaries()'s Supabase-client construction: this
    route runs entirely inside FastAPI/Starlette with no Flask request
    context ever pushed, so that chain cannot fall back to reading Flask's
    global `request` proxy the way Flask-side callers do (plan.md §35.1).
    """
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
            bearer_token,
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

    return {
        "primary_sources": primary_sources,
        "knowledge_rows": knowledge_rows,
        "user_memory_summaries": user_memory_summaries,
        "halachipedia_list": halachipedia_list,
        "wiki_list": wiki_list,
        "customs_info": customs_info,
        "flat_sources_for_ai": flat_sources_for_ai,
        "has_primary_sources": has_primary_sources,
        "has_customs": has_customs,
        "has_whitelisted_external": has_whitelisted_external,
        "use_tertiary_web_context": use_tertiary_web_context,
        "wiki_context_for_ai": wiki_context_for_ai,
        "tool_context": tool_context,
    }


def _ask_async_strict_block(mode, canonical_lens, ctx):
    """Stage 2 of ask_async(): strict-mode guard, or None if the request
    isn't blocked. Split out of ask_async() (SonarCloud python:S3776) --
    see _ask_async_prayer_result.
    """
    if mode != "strict" or ctx["flat_sources_for_ai"]:
        return None

    flask_app_module.DEVTOOLS_STATS["answers_total"] += 1
    flask_app_module.DEVTOOLS_STATS["strict_blocks"] += 1
    flask_app_module.DEVTOOLS_STATS["fallback_answers"] += 1
    display_sources = _compact_ai_sources(ctx["primary_sources"])
    return {
        "answer": (
            "Strict Sources Mode could not complete this request because no primary Sefaria sources "
            "were matched with sufficient confidence. Please refine the question with a text reference."
        ),
        "confidence": 0.2,
        "wiki": ctx["wiki_list"] + ctx["halachipedia_list"],
        "customs": ctx["customs_info"],
        "sources": display_sources,
        "ai_cited_sources": [],
        "meta": {
            "mode": mode,
            "community_lens": canonical_lens,
            "source_count": 0,
            "custom_count": len(ctx["customs_info"]),
            "generated_at": int(time.time()),
            "fallback": True,
            "strict_blocked": True,
            "safety_class": "ok",
            "async": True,
        },
    }


def _ask_async_breaker_paused_payload(mode, canonical_lens, answer_language, ctx):
    """Stage 2.5 of ask_async(): global cost-breaker guard (plan.md §16.3-L3
    / Prompt 29b), or None if the breaker isn't tripped. Mirrors
    _ask_async_strict_block's payload shape -- same DEVTOOLS_STATS bump,
    same source list -- since this is also a no-LLM-call fallback response,
    just triggered by spend rather than source confidence.
    """
    flask_app_module.DEVTOOLS_STATS["answers_total"] += 1
    flask_app_module.DEVTOOLS_STATS["fallback_answers"] += 1
    display_sources = _compact_ai_sources(ctx["primary_sources"])
    return {
        "answer": (
            "AI answers are paused for today -- the full Torah library below is unaffected. "
            "Please try again after midnight UTC, or browse the sources directly."
        ),
        "confidence": 0.0,
        "wiki": ctx["wiki_list"] + ctx["halachipedia_list"],
        "customs": ctx["customs_info"],
        "sources": display_sources,
        "ai_cited_sources": [],
        "meta": {
            "mode": mode,
            "language": answer_language,
            "community_lens": canonical_lens,
            "source_count": len(ctx["primary_sources"]),
            "custom_count": len(ctx["customs_info"]),
            "generated_at": int(time.time()),
            "fallback": True,
            "breaker_tripped": True,
            "safety_class": "ok",
            "async": True,
        },
    }


async def _security_blocked_ask_async_payload(
    result, mode, canonical_lens, answer_language, user_id, question_was_sanitized, question, ctx,
):
    """The "security_blocked" branch of ask_async()'s AI-synthesis stage --
    mirrors app.py's _security_blocked_ask_payload (plan.md §22.3.2's parity
    suite, invariant: identical top-level/meta key set on the security-
    blocked path too, not just success/strict-block/fallback).

    Before this branch existed, ask_async() had no security_blocked handling
    at all: a "security_blocked" result_error was allowed to fall through
    into the success-shaped formatting below, which (a) rendered a safety-
    referral/domain-block message through render_structured_markdown as if
    it were a real halachic answer, (b) reported meta.structured=True for
    what is actually a non-answer, and (c) called _store_user_memory_summary
    on the blocked text, feeding it back into future prompts as if it were a
    genuine past answer. Wrong on a route this safety-critical, and this
    file is the only one of the two live /ask transports actually reachable
    in production (plan.md §16.1-D1) -- app.py's ask_question() never hits
    this bug only because it already special-cased security_blocked.
    """
    structured_payload = result.get("structured")
    if not isinstance(structured_payload, dict):
        structured_payload = None
    safety_class = (structured_payload or {}).get("safety_class", "ok")

    blocked_answer = str(result.get("answer") or "").strip()
    if not blocked_answer:
        blocked_answer = "Request blocked by security policy. Please submit a direct halakhic question."

    flask_app_module.DEVTOOLS_STATS["answers_total"] += 1
    flask_app_module.DEVTOOLS_STATS["fallback_answers"] += 1

    # Defensibility logging (plan.md §8.B.6) -- mirrors app.py's
    # _security_blocked_ask_payload exactly. Deliberately does NOT call
    # _store_user_memory_summary, same reasoning as that function.
    await asyncio.to_thread(
        _store_ask_history,
        user_id,
        question,
        blocked_answer,
        sources=[],
        ai_cited_sources=[],
        community=canonical_lens,
        mode=mode,
        language=answer_language,
        safety_class=safety_class,
        prompt_version=claude.PROMPT_VERSION,
    )

    return {
        "answer": blocked_answer,
        "confidence": result.get("confidence", 0),
        "wiki": [],
        "customs": [],
        "sources": [],
        "ai_cited_sources": [],
        "meta": {
            "mode": mode,
            "community_lens": canonical_lens,
            "source_count": 0,
            "custom_count": 0,
            "knowledge_count": len(ctx["knowledge_rows"]),
            "memory_count": len(ctx["user_memory_summaries"]),
            "identity_aware": bool(user_id),
            "generated_at": int(time.time()),
            "fallback": True,
            "structured": False,
            "is_prohibited": bool((structured_payload or {}).get("is_prohibited", False)),
            "input_sanitized": question_was_sanitized,
            "security": result.get("security") or {},
            "safety_class": safety_class,
            "rabbinic_disclaimer": (structured_payload or {}).get(
                "rabbinic_disclaimer") or claude.RABBI_FINAL_RULING_FOOTER,
            "async": True,
        },
    }


async def _run_ask_async_ai_synthesis(
    question, mode, canonical_lens, answer_language, user_id, question_was_sanitized, ctx,
):
    """Stage 3 of ask_async(): AI synthesis. Raises on any failure -- the
    caller catches and runs _run_ask_async_fallback(). Split out of
    ask_async() (SonarCloud python:S3776) -- see _ask_async_prayer_result.
    """
    tool_context = ctx["tool_context"]
    tool_context = tool_context if isinstance(tool_context, dict) else {
        "route": "/ask", "async": True}
    tool_context["async"] = True

    # AI_AGENTIC_TOOLS (plan.md §9.4, Prompt 20, env-default off) swaps in
    # the agentic tool-use loop; already-async here, so this is a direct
    # coroutine swap with no thread-pool/asyncio.run() indirection needed
    # (contrast app.py's sync call site, which submits to _THREAD_POOL).
    # Off (the default), this branch is never taken and behavior is
    # byte-for-byte the pre-existing claude.ask_ai_async() call.
    _ai_synthesis_coro = (
        ask_pipeline.run_agentic_ask(
            question=question,
            sefaria_sources=ctx["flat_sources_for_ai"],
            customs=ctx["customs_info"],
            user_memories=ctx["user_memory_summaries"],
            wiki=ctx["wiki_context_for_ai"],
            halachipedia=ctx["halachipedia_list"],
            mode=mode,
            community_lens=canonical_lens,
            answer_language=answer_language,
            tool_context=tool_context,
        )
        if claude.AI_AGENTIC_TOOLS
        else claude.ask_ai_async(
            question=question,
            sefaria_sources=ctx["flat_sources_for_ai"],
            customs=ctx["customs_info"],
            user_memories=ctx["user_memory_summaries"],
            wiki=ctx["wiki_context_for_ai"],
            halachipedia=ctx["halachipedia_list"],
            mode=mode,
            community_lens=canonical_lens,
            answer_language=answer_language,
            tool_context=tool_context,
        )
    )
    result = await asyncio.wait_for(
        _ai_synthesis_coro,
        timeout=claude.AI_TOTAL_BUDGET_SECONDS,
    )

    result_error = str(result.get("error") or "")
    if result_error and not result_error.startswith("security_blocked"):
        raise RuntimeError(result_error or "AI request failed")

    if result_error.startswith("security_blocked"):
        return await _security_blocked_ask_async_payload(
            result, mode, canonical_lens, answer_language, user_id,
            question_was_sanitized, question, ctx,
        )

    structured_payload = result.get("structured")
    if not isinstance(structured_payload, dict):
        structured_payload = None

    raw_ai_answer = ""
    if structured_payload:
        raw_ai_answer = claude.render_structured_markdown(
            structured_payload,
            answer_language=answer_language,
            is_simple=bool(result.get("is_simple", False)),
        )
    else:
        raw_ai_answer = str(result.get("answer") or "").strip()

    if not raw_ai_answer:
        raise RuntimeError("AI response was empty")

    # plan.md §9.3 point 3 -- see the matching comment in app.py's
    # _run_ask_question_ai_synthesis for the full rationale.
    if "used_web_search" in result:
        needs_web_warning = bool(result.get("used_web_search"))
    else:
        needs_web_warning = ctx["use_tertiary_web_context"] and bool(
            ctx["wiki_context_for_ai"])

    # The "educational information, not a halachic ruling" disclaimer is
    # shown persistently in the UI banner (renderDisclaimerBanner in
    # templates/index.html) -- it must not also be baked into the answer
    # text itself, or it renders twice.
    normalized_answer = _compose_answer_with_prefixes(
        raw_ai_answer,
        include_web_warning=needs_web_warning,
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
    display_sources = _compact_ai_sources(ctx["primary_sources"])

    ai_cited = extract_ai_cited(structured_payload)

    safety_class = (structured_payload or {}).get("safety_class", "ok")

    await asyncio.to_thread(
        _store_ask_history,
        user_id,
        question,
        normalized_answer,
        sources=display_sources,
        ai_cited_sources=ai_cited,
        community=canonical_lens,
        mode=mode,
        language=answer_language,
        safety_class=safety_class,
        prompt_version=claude.PROMPT_VERSION,
    )

    return {
        "answer": normalized_answer,
        "confidence": result.get("confidence"),
        "wiki": ctx["wiki_list"] + ctx["halachipedia_list"],
        "customs": ctx["customs_info"],
        "sources": display_sources,
        "ai_cited_sources": ai_cited,
        "meta": {
            "mode": mode,
            "language": answer_language,
            "community_lens": canonical_lens,
            "source_count": len(ctx["primary_sources"]),
            "custom_count": len(ctx["customs_info"]),
            "knowledge_count": len(ctx["knowledge_rows"]),
            "memory_count": len(ctx["user_memory_summaries"]),
            "identity_aware": bool(user_id),
            "generated_at": int(time.time()),
            "fallback": bool(result.get("is_fallback", False)),
            "structured": bool(structured_payload),
            "is_prohibited": bool((structured_payload or {}).get("is_prohibited", False)),
            "input_sanitized": question_was_sanitized,
            "security": result.get("security") or {},
            "safety_class": safety_class,
            "rabbinic_disclaimer": (structured_payload or {}).get(
                "rabbinic_disclaimer") or claude.RABBI_FINAL_RULING_FOOTER,
            "async": True,
        },
    }


async def _run_ask_async_fallback(
    question, mode, canonical_lens, answer_language, user_id, ai_error, ctx,
):
    """Stage 4 of ask_async(): halakhic-source-discovery fallback, run when
    _run_ask_async_ai_synthesis() raises. Split out of ask_async()
    (SonarCloud python:S3776) -- see _ask_async_prayer_result.
    """
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

    fallback_answer = _compose_answer_with_prefixes(
        "## Ruling\n\nAI synthesis unavailable. Returning discovered halakhic references.",
        include_web_warning=bool(fallback_warning),
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
        "wiki": ctx["wiki_list"] + ctx["halachipedia_list"],
        "customs": ctx["customs_info"],
        "sources": fallback_sources,
        "ai_cited_sources": [],
        "meta": {
            "mode": mode,
            "language": answer_language,
            "community_lens": canonical_lens,
            "source_count": fallback_payload.get("source_count", 0),
            "custom_count": len(ctx["customs_info"]),
            "knowledge_count": len(ctx["knowledge_rows"]),
            "memory_count": len(ctx["user_memory_summaries"]),
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
                "reason": _coarse_ai_error_reason(ai_error),
            },
            "safety_class": "ok",
            "async": True,
        },
    }


@fastapi_app.post(
    "/ask",
    responses={
        429: {"description": "Rate limit exceeded. Please wait before sending another request."},
        400: {"description": "No valid question provided"},
        401: {"description": "Authentication required"},
        402: {"description": "Daily AI usage limit reached for this account."},
        500: {"description": "An internal error occurred while processing your request"},
    },
)
async def ask_async(
    request: Request,
    payload: AskRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    # Rate limiting (plan.md §16.3-L2) now runs centrally in
    # backend.rate_limit.RateLimitMiddleware, registered on fastapi_app
    # above -- it rejects with 429 before this handler is ever invoked, so
    # there is no in-route check left here (plan.md §16.8.1: this route used
    # to carry its own independent limiter; that duplication is removed).
    client_ip = _get_client_ip(request)
    user_id = extract_user_id_from_bearer_value(authorization)

    question = claude.sanitize_user_query(payload.question)
    question_was_sanitized = question != str(payload.question or "").strip()
    if not question:
        raise HTTPException(
            status_code=400, detail="No valid question provided")

    if CLERK_ENFORCE_AUTH and not user_id:
        raise HTTPException(
            status_code=401, detail="Authentication required")

    bind_user_id(user_id or "")
    bind_client_key("" if user_id else f"ip:{client_ip}")

    # Turnstile gate (plan.md §16.4 / §16.6 Phase 9c, backend/turnstile.py):
    # anonymous only -- a signed-in caller already has a per-account daily
    # quota (backend/rate_limit.py's llm-class authenticated tier) and Clerk
    # signup itself is a much stronger identity signal than a captcha would
    # add on top. True no-op when TURNSTILE_ENABLED is unset (checked inside
    # enforce_anonymous_ask_gate).
    if not user_id:
        turnstile_ok = await _turnstile.enforce_anonymous_ask_gate(
            client_ip, payload.turnstile_token,
        )
        if not turnstile_ok:
            log_mitigation(
                "middleware", "llm", _turnstile.hash_ip(client_ip), "/ask",
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "Verification required before continuing.",
                    "code": "turnstile_required",
                },
            )

    budget = await check_user_budget_and_enforce(user_id, client_ip)
    if not budget["allowed"]:
        raise HTTPException(
            status_code=402,
            detail=(
                "Daily AI usage limit reached for this account "
                f"(${budget['total_usd']:.2f} of ${budget['threshold_usd']:.2f}). "
                "Please try again after midnight UTC."
            ),
        )

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
    # plan.md §14.3.4 / Prompt 29b: identical key formula to app.py's (dead,
    # unreachable-in-production) ask_question() route -- not read/written by
    # that route today, but keeping one formula rather than two avoids a
    # §2 divergent-duplication trap if that route is ever revived.
    ask_cache_key = "|".join([
        question.lower(), answer_language, mode, canonical_lens.lower(), user_id or "anon",
    ])

    try:
        prayer_result = _ask_async_prayer_result(question, mode, canonical_lens)
        if prayer_result is not None:
            return prayer_result

        ctx = await _collect_ask_async_context(
            question, canonical_lens, user_id, answer_language, bearer_token=authorization,
        )

        strict_result = _ask_async_strict_block(mode, canonical_lens, ctx)
        if strict_result is not None:
            return strict_result

        breaker = await is_global_cost_breaker_tripped()
        if breaker["tripped"]:
            cached_payload = flask_app_module._get_cached_ask_payload(ask_cache_key)
            if cached_payload is not None:
                cached_meta = cached_payload.get("meta")
                if isinstance(cached_meta, dict):
                    cached_meta["cached"] = True
                    cached_meta["generated_at"] = int(time.time())
                return cached_payload
            return _ask_async_breaker_paused_payload(mode, canonical_lens, answer_language, ctx)

        try:
            result = await _run_ask_async_ai_synthesis(
                question, mode, canonical_lens, answer_language, user_id,
                question_was_sanitized, ctx,
            )
            flask_app_module._set_cached_ask_payload(ask_cache_key, result)
            return result
        except Exception as ai_error:
            return await _run_ask_async_fallback(
                question, mode, canonical_lens, answer_language, user_id,
                ai_error, ctx,
            )

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
            status_code=500, detail="An internal error occurred while processing your request.")


# Mount existing Flask app so all legacy routes continue to work.
fastapi_app.mount("/", WSGIMiddleware(flask_app_module.app))

# Export canonical ASGI application.
app = fastapi_app
