"""
LLM and translation API cost tracking for Sh'elah.

Records token usage and estimated USD cost for every outbound AI call,
then writes to Supabase ai_usage_log table via a fire-and-forget background
thread so it never blocks the request path.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# ── Price table (USD per 1M tokens) ─────────────────────────────────────────
# Update when Anthropic/Google adjust pricing.
_PRICE_PER_M = {
    # Anthropic Claude 4 family
    "claude-sonnet-4-6":   {"input": 3.00,  "output": 15.00},
    "claude-opus-4-8":     {"input": 15.00, "output": 75.00},
    "claude-haiku-4-5":    {"input": 0.80,  "output": 4.00},
    # Legacy / fallback
    "claude-3-5-sonnet":   {"input": 3.00,  "output": 15.00},
    "claude-3-opus":       {"input": 15.00, "output": 75.00},
    "claude-3-haiku":      {"input": 0.25,  "output": 1.25},
    # Google Gemini
    "gemini-1.5-flash":    {"input": 0.075, "output": 0.30},
    "gemini-1.5-pro":      {"input": 3.50,  "output": 10.50},
    "gemini-2.0-flash":    {"input": 0.10,  "output": 0.40},
    # Translation (per-call flat estimate, not token-based)
    "google-translate":    {"input": 0.0,   "output": 0.02},
    "mymemory":            {"input": 0.0,   "output": 0.0},
}

_UNKNOWN_PRICE = {"input": 0.0, "output": 0.0}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return estimated USD cost for a single call."""
    prices = _PRICE_PER_M.get(model.lower(), _UNKNOWN_PRICE)
    return (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000


def _insert_usage_row(row: dict[str, Any]) -> None:
    """Synchronous Supabase insert — called via asyncio.to_thread."""
    try:
        from app import _get_supabase_client  # lazy import to avoid circular at module load
        client = _get_supabase_client()
        if client is None:
            return
        client.table("ai_usage_log").insert(row).execute()
    except Exception as exc:
        logger.debug("cost_meter insert skipped: %s", exc)


async def record_llm_call(
    *,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    route: str = "",
    request_id: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """
    Fire-and-forget background write to ai_usage_log.

    Safe to await anywhere — the Supabase insert runs in a thread so it
    never blocks the asyncio event loop.
    """
    cost = estimate_cost_usd(model, input_tokens, output_tokens)
    row: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round(cost, 8),
        "route": route,
        "request_id": request_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if extra:
        row.update({k: v for k, v in extra.items() if k not in row})

    logger.debug(
        "llm_call provider=%s model=%s in=%d out=%d cost=$%.6f",
        provider, model, input_tokens, output_tokens, cost,
    )

    try:
        await asyncio.to_thread(_insert_usage_row, row)
    except Exception as exc:
        logger.debug("cost_meter background write failed: %s", exc)
