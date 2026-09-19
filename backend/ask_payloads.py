"""Response-body builders shared by the sync (app.py) and async (asgi.py) /ask paths.

The Flask WSGI route and the FastAPI route answer the same question the same
way and return the same JSON; only the transport differs. The two dictionaries
below used to be written out twice (once per transport). They live here so the
response shape has one definition.

Each builder takes ``extra_meta``: the few transport-specific ``meta`` keys
(``{"cached": False}`` for Flask, ``{"async": True}`` for ASGI), appended last
so key order on the wire is unchanged.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from backend import claude
from backend.helpers import _coarse_ai_error_reason


def build_ai_answer_payload(
    *,
    result: Dict[str, Any],
    answer: str,
    sources: list,
    ai_cited: list,
    structured_payload: Optional[Dict[str, Any]],
    ctx: Dict[str, Any],
    mode: str,
    answer_language: str,
    canonical_lens: str,
    user_id: Optional[str],
    question_was_sanitized: bool,
    extra_meta: Dict[str, Any],
) -> Dict[str, Any]:
    """The payload for a successful AI answer."""
    structured = structured_payload or {}
    return {
        "answer": answer,
        "confidence": result.get("confidence"),
        "wiki": ctx["wiki_list"] + ctx["halachipedia_list"],
        "customs": ctx["customs_info"],
        "sources": sources,
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
            "is_prohibited": bool(structured.get("is_prohibited", False)),
            "input_sanitized": question_was_sanitized,
            "security": result.get("security") or {},
            "safety_class": structured.get("safety_class", "ok"),
            "rabbinic_disclaimer": structured.get(
                "rabbinic_disclaimer") or claude.RABBI_FINAL_RULING_FOOTER,
            **extra_meta,
        },
    }


def build_source_fallback_payload(
    *,
    answer: str,
    sources: list,
    discovery: Dict[str, Any],
    warning: str,
    ai_error: BaseException,
    ctx: Dict[str, Any],
    mode: str,
    answer_language: str,
    canonical_lens: str,
    user_id: Optional[str],
    extra_meta: Dict[str, Any],
) -> Dict[str, Any]:
    """The payload returned when AI synthesis failed and the answer is the
    halakhic-source-discovery fallback. ``discovery`` is what
    get_halakhic_sources() returned; ``warning`` is its stripped warning."""
    return {
        "answer": answer,
        "confidence": 0.4,
        "wiki": ctx["wiki_list"] + ctx["halachipedia_list"],
        "customs": ctx["customs_info"],
        "sources": sources,
        "ai_cited_sources": [],
        "meta": {
            "mode": mode,
            "language": answer_language,
            "community_lens": canonical_lens,
            "source_count": discovery.get("source_count", 0),
            "custom_count": len(ctx["customs_info"]),
            "knowledge_count": len(ctx["knowledge_rows"]),
            "memory_count": len(ctx["user_memory_summaries"]),
            "identity_aware": bool(user_id),
            "generated_at": int(time.time()),
            "fallback": True,
            "status": discovery.get("status", "fallback"),
            "fallback_detail": {
                "keywords": discovery.get("keywords", []),
                "sequence": discovery.get("sequence", []),
                "counts": discovery.get("counts", {}),
                "level": discovery.get("fallback_level", "unknown"),
                "warning": warning,
                "reason": _coarse_ai_error_reason(ai_error),
            },
            "safety_class": "ok",
            **extra_meta,
        },
    }
