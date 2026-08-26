"""
LLM and translation API cost tracking for Sh'elah.

Records token usage and estimated USD cost for every outbound AI call,
then writes to Supabase ai_usage_log table via a fire-and-forget background
thread so it never blocks the request path.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from backend.logging_setup import (
    _capture_backend_error,
    bind_budget_reservation,
    get_budget_reservation,
    get_client_key,
    get_user_id,
)

try:
    import sentry_sdk
except Exception:  # pragma: no cover
    sentry_sdk = None

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
    # Production primary model (backend/claude.py:_DEFAULT_GEMINI_MODEL).
    # Verified against ai.google.dev/gemini-api/docs/pricing 2026-08-19 --
    # was missing entirely (plan.md §20.1-C1), which meant every Gemini
    # /ask call was logged to ai_usage_log at cost_usd=0.0.
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50},
    # Translation (per-call flat estimate, not token-based)
    "google-translate":    {"input": 0.0,   "output": 0.02},
    "mymemory":            {"input": 0.0,   "output": 0.0},
}

_UNKNOWN_PRICE = {"input": 0.0, "output": 0.0}

# Tracks which unpriced model names have already produced a warning this
# process, so a hot /ask path doesn't spam one WARNING log line per call.
_WARNED_UNPRICED_MODELS: set[str] = set()


def _warn_unpriced_model_once(model: str) -> None:
    """Surface a price-table gap loudly (plan.md §20.1-C1): the original bug
    was never the $0.0 arithmetic (that's correct behavior for a genuinely
    unknown model) -- it was that the gap was invisible. Fires once per
    process per model name; still returns $0.0, never guesses a price."""
    key = model.lower()
    if key in _WARNED_UNPRICED_MODELS:
        return
    _WARNED_UNPRICED_MODELS.add(key)
    logger.warning(
        "cost_meter: no price entry for model=%r -- cost_usd will be logged "
        "as 0.0 for every call to this model until _PRICE_PER_M is updated.",
        model,
    )
    if sentry_sdk is not None:
        try:
            sentry_sdk.add_breadcrumb(
                category="cost_meter",
                message=f"Unpriced model: {model}",
                level="warning",
            )
        except Exception:
            pass


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return estimated USD cost for a single call."""
    key = (model or "").lower()
    prices = _PRICE_PER_M.get(key)
    if prices is None:
        _warn_unpriced_model_once(model)
        prices = _UNKNOWN_PRICE
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
        # Was logger.debug -- invisible at the project's default LOG_LEVEL=
        # INFO, which meant a schema mismatch/revoked key/paused project
        # could silently zero the entire cost ledger with no signal
        # anywhere (plan.md §20.1-C3b, same failure shape as the
        # accept_legal()/clerk_id bug in §23.1). Routed through the
        # project's structured-error funnel instead; the write itself
        # stays non-fatal to the request -- this changes visibility, not
        # control flow.
        _capture_backend_error(
            "cost_meter_insert_failed",
            exc,
            {
                "provider": row.get("provider"),
                "model": row.get("model"),
                "route": row.get("route"),
            },
        )


def _settle_usage_reservation(reservation_id: str, row: dict[str, Any]) -> None:
    """Synchronous Supabase update — called via asyncio.to_thread.

    Replaces a check_and_reserve_user_budget() placeholder row (plan.md
    §20.2 Phase 20b, reservation lifecycle step 2) with the real
    provider/model/token counts/cost from the completed call, clearing
    `reserved` so it reads as a normal settled row from then on — this
    does NOT insert a second row, which would double-count the spend
    Prompt 33a's price-table fix made real.

    created_at is deliberately excluded from the update: the row keeps the
    timestamp of when the budget was actually reserved (the day it counted
    against), not when the call happened to finish.
    """
    try:
        from app import _get_supabase_client  # lazy import to avoid circular at module load
        client = _get_supabase_client()
        if client is None:
            return
        settle_row = {k: v for k, v in row.items() if k != "created_at"}
        settle_row["reserved"] = False
        result = (
            client.table("ai_usage_log")
            .update(settle_row)
            .eq("reservation_id", reservation_id)
            .execute()
        )
        if not (result.data or []):
            # Reservation row no longer exists (TTL-expired and swept by
            # expire_stale_budget_reservations(), or never created) --
            # still record the real spend rather than silently dropping it.
            _insert_usage_row(row)
    except Exception as exc:
        _capture_backend_error(
            "cost_meter_settle_reservation_failed",
            exc,
            {
                "reservation_id": reservation_id,
                "model": row.get("model"),
                "route": row.get("route"),
            },
        )
        # Last-resort: still record the real spend as a normal row.
        # _insert_usage_row() catches its own exceptions, so this cannot
        # raise a second time.
        _insert_usage_row(row)


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

    Safe to await anywhere — the Supabase write runs in a thread so it
    never blocks the asyncio event loop.

    If check_user_budget_and_enforce() reserved budget for this request
    (plan.md §20.2 Phase 20b), this SETTLES that reservation in place with
    the real cost instead of inserting a second row -- and clears the
    reservation so a second billed call in the same request (e.g. a Gemini
    primary call followed by a Claude fallback) inserts a normal additive
    row rather than re-settling.
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
    row.setdefault("user_id", get_user_id())
    row.setdefault("client_key", get_client_key())

    logger.debug(
        "llm_call provider=%s model=%s in=%d out=%d cost=$%.6f",
        provider, model, input_tokens, output_tokens, cost,
    )

    reservation_id = get_budget_reservation()
    try:
        if reservation_id:
            await asyncio.to_thread(_settle_usage_reservation, reservation_id, row)
            bind_budget_reservation("")
        else:
            await asyncio.to_thread(_insert_usage_row, row)
    except Exception as exc:
        logger.debug("cost_meter background write failed: %s", exc)


# ── Daily budget alert ───────────────────────────────────────────────────────

_DAILY_BUDGET_ENV = "DAILY_BUDGET_USD"


def _daily_budget_usd() -> float:
    """Configured daily spend cap, or 0.0 (disabled) if unset/invalid."""
    raw = (os.environ.get(_DAILY_BUDGET_ENV) or "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


_USAGE_FETCH_PAGE_SIZE = 1000
# Safety backstop against a pathological infinite loop (e.g. a broken/mocked
# .range() that keeps returning full pages) — 200 pages * 1000 rows/page is
# far beyond any realistic single-day call volume for this project.
_USAGE_FETCH_MAX_PAGES = 200


def _fetch_today_usage_rows() -> list[dict[str, Any]]:
    """Synchronous Supabase read — called via asyncio.to_thread.

    Paginates via .range() rather than a single unbounded .select(): Supabase/
    PostgREST applies a default max-rows cap per request (commonly 1000), so
    an un-paginated query would silently undercount total spend on any day
    with more calls than that cap — exactly the high-traffic days this
    budget check exists to catch.
    """
    try:
        from app import _get_supabase_client  # lazy import to avoid circular at module load
        client = _get_supabase_client()
        if client is None:
            return []
        start_of_day = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")

        rows: list[dict[str, Any]] = []
        offset = 0
        for _ in range(_USAGE_FETCH_MAX_PAGES):
            result = (
                client.table("ai_usage_log")
                .select("cost_usd")
                .gte("created_at", start_of_day)
                .range(offset, offset + _USAGE_FETCH_PAGE_SIZE - 1)
                .execute()
            )
            page = result.data if isinstance(result.data, list) else []
            rows.extend(page)
            if len(page) < _USAGE_FETCH_PAGE_SIZE:
                return rows
            offset += _USAGE_FETCH_PAGE_SIZE

        logger.warning(
            "check_daily_budget usage fetch hit the %d-page safety cap "
            "(%d rows) — daily total may be undercounted",
            _USAGE_FETCH_MAX_PAGES, len(rows),
        )
        return rows
    except Exception as exc:
        logger.debug("check_daily_budget usage fetch skipped: %s", exc)
        return []


async def check_daily_budget_and_alert() -> dict[str, Any]:
    """
    Sum today's (UTC calendar day) ai_usage_log cost and alert through
    _capture_backend_error (structured log + webhook + Sentry) when it
    reaches or exceeds DAILY_BUDGET_USD.

    Opt-in: returns configured=False and does no Supabase read at all when
    DAILY_BUDGET_USD isn't set, so this is a no-op until an operator sets a
    cap. Intended to be invoked once daily by a Vercel Cron job.

    NOTE (plan.md §16.3 L3 / Prompt 29b): this is an after-the-fact ALERT
    (cron-invoked, once/day, never blocks a call) -- the enforcement CEILING
    that phase specifies (checked before every provider call, trips a
    circuit breaker) is is_global_cost_breaker_tripped() below. Both read
    the same _daily_budget_usd()/_fetch_today_usage_rows() primitives
    rather than maintaining two independent daily-total computations (§2
    divergent-duplication anti-pattern).
    """
    threshold = _daily_budget_usd()
    if threshold <= 0:
        return {
            "configured": False,
            "total_usd": 0.0,
            "threshold_usd": 0.0,
            "call_count": 0,
            "exceeded": False,
        }

    rows = await asyncio.to_thread(_fetch_today_usage_rows)
    valid_rows = [row for row in rows if isinstance(row, dict)]
    total_usd = sum(float(row.get("cost_usd") or 0.0) for row in valid_rows)
    call_count = len(valid_rows)
    exceeded = total_usd >= threshold

    if exceeded:
        _capture_backend_error(
            "daily_ai_budget_exceeded",
            RuntimeError(
                f"Daily AI spend ${total_usd:.4f} has reached the "
                f"${threshold:.4f} DAILY_BUDGET_USD cap ({call_count} calls today)."
            ),
            {
                "total_usd": round(total_usd, 8),
                "threshold_usd": threshold,
                "call_count": call_count,
            },
        )

    return {
        "configured": True,
        "total_usd": round(total_usd, 8),
        "threshold_usd": threshold,
        "call_count": call_count,
        "exceeded": exceeded,
    }


# ── Global cost circuit breaker (plan.md §16.3-L3 / Prompt 29b) ─────────────
# Checked before every /ask provider call (asgi.py::ask_async), unlike
# check_daily_budget_and_alert() above which only runs once/day via cron.
# A Supabase read on every single call would defeat its own purpose -- it
# would add real latency (and its own cost) to the hot path it's meant to
# protect -- so the computed total is cached for a short TTL in the same
# shared store backend/rate_limit.py already maintains (Redis in production,
# in-process fallback locally), rather than opening a second store or
# re-deriving a fresh total on every request.
_GLOBAL_BREAKER_CACHE_KEY = "cost_breaker:global:tripped_total"
_GLOBAL_BREAKER_CACHE_TTL_SECONDS = 30


async def is_global_cost_breaker_tripped() -> dict[str, Any]:
    """True once today's global AI spend reaches DAILY_BUDGET_USD.

    Opt-in like check_daily_budget_and_alert(): returns configured=False
    (never tripped) when DAILY_BUDGET_USD isn't set. Fails OPEN if the
    Supabase read itself fails or the shared cache is unreachable -- the
    same posture every other Supabase-touching function in this module
    already takes (_fetch_today_usage_rows, _reserve_budget_or_deny,
    expire_stale_budget_reservations), so a transient outage degrades to
    "no global ceiling today" rather than pausing /ask for every user.
    Provider-side (Anthropic/Google) spend caps remain the real backstop
    per docs/RUNBOOKS.md; this is a soft guardrail on top, not the last
    line of defense, so failing open here does not leave spend unbounded.
    """
    threshold = _daily_budget_usd()
    if threshold <= 0:
        return {"tripped": False, "total_usd": 0.0, "threshold_usd": 0.0, "configured": False}

    # Lazy import to avoid a circular import at module load: backend.rate_limit
    # -> backend.helpers -> backend.utils.search_provider -> backend.claude
    # -> backend.cost_meter (record_llm_call). By the time this async function
    # actually runs, every module in that cycle has finished initializing.
    from backend.rate_limit import get_shared_store

    store = get_shared_store()
    cached = None
    try:
        cached = await store.get(_GLOBAL_BREAKER_CACHE_KEY)
    except Exception as exc:
        logger.debug("cost breaker cache read skipped: %s", exc)

    if cached is not None:
        try:
            cached_total = float(cached)
            return {
                "tripped": cached_total >= threshold,
                "total_usd": round(cached_total, 8),
                "threshold_usd": threshold,
                "configured": True,
            }
        except ValueError:
            pass  # corrupt cache value -- fall through and recompute

    rows = await asyncio.to_thread(_fetch_today_usage_rows)
    valid_rows = [row for row in rows if isinstance(row, dict)]
    total_usd = sum(float(row.get("cost_usd") or 0.0) for row in valid_rows)

    try:
        await store.setex(_GLOBAL_BREAKER_CACHE_KEY, _GLOBAL_BREAKER_CACHE_TTL_SECONDS, str(total_usd))
    except Exception as exc:
        logger.debug("cost breaker cache write skipped: %s", exc)

    tripped = total_usd >= threshold
    if tripped:
        _capture_backend_error(
            "global_cost_breaker_tripped",
            RuntimeError(
                f"Global AI spend ${total_usd:.4f} has reached the "
                f"${threshold:.4f} DAILY_BUDGET_USD ceiling -- /ask is now paused."
            ),
            {"total_usd": round(total_usd, 8), "threshold_usd": threshold},
        )

    return {"tripped": tripped, "total_usd": round(total_usd, 8), "threshold_usd": threshold, "configured": True}


# ── Per-caller budget ceiling (request-path circuit breaker) ────────────────
# Unlike check_daily_budget_and_alert() above (global total, cron-invoked
# once/day, alert-only), this is checked before every AI provider call and
# actually blocks the request — the enforcement mechanism referenced but not
# implemented by that function's docstring (plan.md §16.3 L3 / Prompt 29b).

_PER_USER_DAILY_BUDGET_ENV = "PER_USER_DAILY_BUDGET_USD"
# Ships enabled by default (rather than opt-in like DAILY_BUDGET_USD) so the
# "no ceiling on AI spend" gap is closed out of the box; set to "0" to
# disable, or override via env for a different per-caller cap.
_DEFAULT_PER_USER_DAILY_BUDGET_USD = 2.00


def _per_user_daily_budget_usd() -> float:
    """Configured per-caller daily spend cap (defaults to
    _DEFAULT_PER_USER_DAILY_BUDGET_USD; set the env var to "0" to disable)."""
    raw = (os.environ.get(_PER_USER_DAILY_BUDGET_ENV) or "").strip()
    if not raw:
        return _DEFAULT_PER_USER_DAILY_BUDGET_USD
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_PER_USER_DAILY_BUDGET_USD


def _fetch_today_usage_cost_for_key(key_column: str, key_value: str) -> float:
    """Synchronous Supabase read — called via asyncio.to_thread.

    Fails open (returns 0.0) on any error, including "column doesn't
    exist" if the ai_usage_log table predates the user_id/client_key
    columns (see scripts/migrate_ai_usage_log_add_user_columns.sql) — a
    misconfigured/un-migrated table degrades to "no cap enforced", not a
    broken /ask endpoint.

    No longer called by check_user_budget_and_enforce() (plan.md §20.2
    Phase 20b replaced that read-then-decide race with the atomic
    _reserve_budget_or_deny() RPC below) — kept as a plain read helper for
    any future "show my usage today" surface; still exercised directly by
    tests/test_cost_meter.py.
    """
    try:
        from app import _get_supabase_client  # lazy import to avoid circular at module load
        client = _get_supabase_client()
        if client is None or not key_value:
            return 0.0
        start_of_day = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
        result = (
            client.table("ai_usage_log")
            .select("cost_usd")
            .eq(key_column, key_value)
            .gte("created_at", start_of_day)
            .execute()
        )
        rows = result.data if isinstance(result.data, list) else []
        return sum(
            float(row.get("cost_usd") or 0.0)
            for row in rows
            if isinstance(row, dict)
        )
    except Exception as exc:
        logger.debug("per-caller budget check fetch skipped: %s", exc)
        return 0.0


_RESERVATION_TTL_MINUTES = 10  # must match scripts/sql/check_and_reserve_user_budget.sql

# Worst-case single /ask call, used to reserve budget BEFORE dispatch
# (plan.md §20.1-C2's second property: "a caller starting the day at $0
# always gets one unbounded call through" — a running-total cap alone never
# closes this, only a per-call reservation does).
#
# Output: 3072 tokens (backend/claude.py's non-simple max_tokens, the
# primary Gemini call) + 1024 tokens (the Claude fallback's max_tokens, if
# it also fires in the same request) = 4096, priced at the pricier of the
# two dispatchable non-Gemini-default output rates (claude-haiku-4-5,
# $4.00/1M) so the reservation covers either provider.
# Input: CORE_SYSTEM_PROMPT (~5.1K chars) + the 2200-char sanitized dynamic
# context cap (backend/claude.py::_sanitize_prompt_payload) + the 1200-char
# MAX_INPUT_CHARS question cap ≈ 8500 chars, padded to 6000 tokens at a
# conservative ~1.4 chars/token (denser than plain-English ~4 chars/token,
# to cover Hebrew-heavy prompts).
_MAX_SINGLE_ASK_OUTPUT_TOKENS = 4096
_MAX_SINGLE_ASK_INPUT_TOKENS = 6000
_MAX_SINGLE_ASK_INPUT_PRICE_PER_M = 0.80   # claude-haiku-4-5 input rate
_MAX_SINGLE_ASK_OUTPUT_PRICE_PER_M = 4.00  # claude-haiku-4-5 output rate


def _max_single_ask_reservation_usd() -> float:
    """Worst-case USD cost of one /ask call — reserved atomically before
    dispatch so a single call can never blow through the per-caller ceiling
    unbounded, regardless of how the running total is doing."""
    return (
        _MAX_SINGLE_ASK_INPUT_TOKENS * _MAX_SINGLE_ASK_INPUT_PRICE_PER_M
        + _MAX_SINGLE_ASK_OUTPUT_TOKENS * _MAX_SINGLE_ASK_OUTPUT_PRICE_PER_M
    ) / 1_000_000


def _reserve_budget_or_deny(
    key_column: str, key_value: str, threshold_usd: float, reservation_usd: float
) -> dict[str, Any]:
    """Synchronous Supabase RPC call — called via asyncio.to_thread.

    Atomic check-and-reserve (plan.md §20.1-C2 fix): the read and the write
    happen in ONE Postgres statement (scripts/sql/
    check_and_reserve_user_budget.sql), so concurrent callers for the same
    key see each other's reservations instead of racing on a stale read —
    unlike the old _fetch_today_usage_cost_for_key(), which only ever read.

    Fails OPEN on transport errors (same posture as the function it
    replaces): a Supabase hiccup, or the migration not yet applied,
    degrades to "uncapped for now", never a 500 on /ask.
    """
    try:
        from app import _get_supabase_client  # lazy import to avoid circular at module load
        client = _get_supabase_client()
        if client is None or not key_value:
            return {"allowed": True, "total_usd": 0.0, "reservation_id": ""}
        result = client.rpc(
            "check_and_reserve_user_budget",
            {
                "p_key_column": key_column,
                "p_key_value": key_value,
                "p_threshold_usd": threshold_usd,
                "p_reservation_usd": reservation_usd,
            },
        ).execute()
        # A set-returning RPC (our SQL function is TABLE(...)) always comes
        # back from PostgREST as a JSON array, even for one row -- never a
        # bare dict. Require that shape AND the "allowed" key explicitly, so
        # an unrecognized/malformed response (e.g. a mocked or misconfigured
        # client that returns some other envelope) falls into the same
        # fail-open branch below instead of having bool(None) silently
        # read as a deny.
        rows = result.data if isinstance(result.data, list) else None
        if not rows or not isinstance(rows[0], dict) or "allowed" not in rows[0]:
            return {"allowed": True, "total_usd": 0.0, "reservation_id": ""}
        row = rows[0]
        return {
            "allowed": bool(row.get("allowed")),
            "total_usd": float(row.get("total_usd") or 0.0),
            "reservation_id": str(row.get("reservation_id") or ""),
        }
    except Exception as exc:
        logger.debug("atomic per-caller budget reserve skipped: %s", exc)
        return {"allowed": True, "total_usd": 0.0, "reservation_id": ""}


async def check_user_budget_and_enforce(user_id: str | None, client_ip: str = "") -> dict[str, Any]:
    """
    Enforce a per-caller daily AI spend ceiling BEFORE dispatching to a
    model, atomically (plan.md §20.2 Phase 20b): the check and the budget
    reservation happen in one Postgres statement, closing the check-then-
    act race a plain read had (plan.md §20.1-C2 — N concurrent callers near
    the cap could otherwise all read the same pre-spend total and all
    pass). Keys by authenticated user_id when available; falls back to a
    per-IP key for anonymous callers so unauthenticated /ask access still
    has a cost ceiling.

    On success, binds the reservation id into the request's contextvar
    (backend/logging_setup.py::bind_budget_reservation) so record_llm_call()
    can SETTLE it with the real cost once the AI call completes, instead of
    inserting a second ai_usage_log row.

    Returns {"allowed": bool, "total_usd": float, "threshold_usd": float}.
    Fails open (allowed=True) if the reserve call itself errors, so a
    Supabase hiccup degrades to "uncapped for now", not a 500 on /ask.
    """
    threshold = _per_user_daily_budget_usd()
    if threshold <= 0:
        return {"allowed": True, "total_usd": 0.0, "threshold_usd": 0.0}

    if user_id:
        key_column, key_value = "user_id", user_id
    elif client_ip:
        key_column, key_value = "client_key", f"ip:{client_ip}"
    else:
        return {"allowed": True, "total_usd": 0.0, "threshold_usd": threshold}

    reservation_usd = _max_single_ask_reservation_usd()
    result = await asyncio.to_thread(
        _reserve_budget_or_deny, key_column, key_value, threshold, reservation_usd,
    )
    if result["allowed"] and result["reservation_id"]:
        bind_budget_reservation(result["reservation_id"])
    return {
        "allowed": result["allowed"],
        "total_usd": round(result["total_usd"], 8),
        "threshold_usd": threshold,
    }


def expire_stale_budget_reservations() -> dict[str, Any]:
    """Delete abandoned budget-reservation rows (plan.md §20.2 Phase 20b,
    reservation lifecycle step 3): a process that dies between RESERVE and
    SETTLE (crash, cold-start eviction, request timeout) leaves a
    `reserved=true` row that would otherwise count against that caller's
    budget forever. Deletes rather than settles — no real spend happened,
    so the reserved amount must be released, not recorded.

    Safe to call repeatedly (idempotent). Intended to be invoked from the
    existing retention-enforce cron (backend/routes_privacy.py::
    retention_enforce) rather than a new scheduled job, per plan.md §20.2's
    explicit recommendation.
    """
    try:
        from app import _get_supabase_client  # lazy import to avoid circular at module load
        client = _get_supabase_client()
        if client is None:
            return {"deleted": 0}
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = (
            client.table("ai_usage_log")
            .delete()
            .eq("reserved", True)
            .lt("reservation_expires_at", now_iso)
            .execute()
        )
        deleted = len(result.data or [])
        if deleted:
            logger.warning("expired %d stale AI-spend budget reservation(s)", deleted)
        return {"deleted": deleted}
    except Exception as exc:
        _capture_backend_error("budget_reservation_expiry_failed", exc, {})
        return {"deleted": 0, "error": str(exc)}
