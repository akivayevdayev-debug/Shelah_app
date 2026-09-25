"""Cost gates + spend attribution for sync (Flask WSGI) model-calling routes.

asgi.py's async /ask awaits cost_meter's two gates directly and binds the
caller's identity at the top of the handler. A sync Flask view can do
neither on its own: the gates are coroutines, and nothing on the WSGI path
binds the contextvars record_llm_call() (backend/cost_meter.py) reads when
it writes an ai_usage_log row -- so spend from a sync route was logged with
an empty user_id and never counted toward the per-user daily cap.

This module is the sync-side counterpart: one loop-bridge hop that runs
both gates, plus the bind/clear pair that attributes the request's model
spend. Shared by app.py's sync /ask (_apply_ask_question_cost_gates) and
backend/routes_conversations.py's conversation ask
(_enforce_conversation_cost_gates); each route maps the verdicts to its own
responses and decides whether a failed gate hop fails open.
"""
from __future__ import annotations

import asyncio
from typing import Any

from backend import claude
from backend.cost_meter import check_user_budget_and_enforce, is_global_cost_breaker_tripped
from backend.logging_setup import (
    bind_budget_reservation,
    bind_client_key,
    bind_user_id,
    get_budget_reservation,
    submit_with_context,
)

# Backstop on the loop-bridge hop for the two gate reads (one budget RPC +
# one usage read/cache hit) -- well above their normal latency, far below
# the model call's own budget, so a wedged Supabase read can't pin the
# request thread indefinitely.
COST_GATE_TIMEOUT_SECONDS = 20.0


def client_key_for(user_id: str | None, client_ip: str) -> str:
    """The anonymous-caller key cost_meter budgets signed-out callers by
    ("ip:<addr>"), or "" for a signed-in caller (keyed by user_id instead)
    or when no client IP could be resolved (nothing to key by)."""
    if user_id or not client_ip:
        return ""
    return f"ip:{client_ip}"


async def _evaluate_cost_gates(user_id: str | None, client_ip: str) -> dict[str, Any]:
    """Both gates in one coroutine, so the sync route pays a single
    loop-bridge hop. The global breaker is checked FIRST (unlike asgi.py's
    ask_async, which reserves per-user budget before its breaker check):
    check_user_budget_and_enforce() atomically RESERVES budget on success,
    and a reservation made for an ask the breaker then refuses would sit
    against the caller's daily cap until expire_stale_budget_reservations()
    sweeps it.

    Returns the reservation id alongside the verdicts because
    check_user_budget_and_enforce() binds it into a contextvar -- and that
    binding lives in asyncio.run()'s task context on the bridge thread, so
    it would otherwise never reach the request's synthesis call."""
    breaker = await is_global_cost_breaker_tripped()
    if breaker["tripped"]:
        return {"tripped": True, "budget": None, "reservation_id": ""}
    budget = await check_user_budget_and_enforce(user_id, client_ip)
    return {"tripped": False, "budget": budget, "reservation_id": get_budget_reservation()}


def evaluate_cost_gates_sync(user_id: str | None, client_ip: str) -> dict[str, Any]:
    """Run _evaluate_cost_gates() from a sync request thread, on
    backend/claude.py's existing loop-bridge executor (the same
    submit_with_context(..., asyncio.run, coro) hop _call_primary_model_sync's
    escape hatch uses) rather than spinning up an event loop in the request
    thread. Raises concurrent.futures.TimeoutError past
    COST_GATE_TIMEOUT_SECONDS."""
    # Cleared first so the context copied onto the bridge thread can't carry
    # a stale reservation id back out when the per-user cap is disabled.
    bind_budget_reservation("")
    future = submit_with_context(
        claude._get_loop_bridge_executor(), asyncio.run, _evaluate_cost_gates(user_id, client_ip))
    return future.result(timeout=COST_GATE_TIMEOUT_SECONDS)


def budget_exhausted_message(budget: dict[str, Any]) -> str:
    """Same wording as asgi.py's _enforce_ask_async_budget 402."""
    return (
        "Daily AI usage limit reached for this account "
        f"(${budget['total_usd']:.2f} of ${budget['threshold_usd']:.2f}). "
        "Please try again after midnight UTC."
    )


def bind_cost_attribution(user_id: str | None, client_ip: str, reservation_id: str = "") -> None:
    """Bind the caller's identity and budget reservation into THIS request's
    context. The model call carries that context into its worker via
    submit_with_context, which is how record_llm_call() tags the
    ai_usage_log row with this caller and settles the reservation."""
    bind_user_id(user_id or "")
    bind_client_key(client_key_for(user_id, client_ip))
    bind_budget_reservation(reservation_id)


def clear_cost_attribution() -> None:
    """Undo bind_cost_attribution(), so a reused worker thread (e.g. Flask's
    threaded dev server, which doesn't run each request in a fresh context
    copy the way Starlette's WSGIMiddleware does) can't attribute -- or
    settle a leftover reservation for -- a later request's model spend to
    this caller."""
    bind_user_id("")
    bind_client_key("")
    bind_budget_reservation("")
