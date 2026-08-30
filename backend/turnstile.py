"""
Cloudflare Turnstile gate for anonymous /ask traffic (plan.md §16.3-L2 /
§16.4 / §16.6 Phase 9c).

WHY TURNSTILE, NOT VERCEL BOTID (plan.md §16.4): BotID's server-side
verification SDK is JavaScript-only -- there is no Python entry point at
any Vercel plan tier, and /ask's request handling is entirely Python
(asgi.py's FastAPI route). BotID therefore cannot be wired into this
endpoint regardless of budget. Turnstile is backend-agnostic: verification
is one plain HTTPS POST (`siteverify`) any language can make, it is free at
Sh'elah's volume, and it is invisible to the vast majority of legitimate
callers (Cloudflare's managed challenge only renders a visible widget for
traffic it already suspects).

This module owns two independent things:
  1. A per-anonymous-IP hourly threshold (backend.rate_limit's shared store
     -- plan.md §3 reuse rule, not a second store) that decides whether a
     challenge is required at all. Most anonymous callers never cross it.
  2. The `siteverify` HTTP call that checks a submitted token once a
     challenge *is* required.

Feature-flagged via TURNSTILE_ENABLED (default off): every function below
is a true no-op when unset, so local dev and the test suite are unaffected
by default -- matching backend/cost_meter.py's DAILY_BUDGET_USD /
backend/rate_limit.py's RATE_LIMIT_REDIS_URL opt-in pattern in this same
codebase.
"""

from __future__ import annotations

import hashlib
import logging
import os

logger = logging.getLogger(__name__)

_SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

TURNSTILE_ENABLED = (os.environ.get("TURNSTILE_ENABLED") or "").strip().lower() == "true"
# Server secret -- never sent to the browser. Required once TURNSTILE_ENABLED
# is true; if the flag is on but this is empty, verify_turnstile_token()
# fails CLOSED (see its docstring) rather than silently granting a bypass.
TURNSTILE_SECRET_KEY = (os.environ.get("TURNSTILE_SECRET_KEY") or "").strip()
# Public site key, read here only so it can be surfaced to the frontend via
# an existing config endpoint if/when the widget itself is built -- this
# module does not render anything. Not a secret.
TURNSTILE_SITE_KEY = (os.environ.get("TURNSTILE_SITE_KEY") or "").strip()

try:
    TURNSTILE_ANON_HOURLY_THRESHOLD = int(
        os.environ.get("TURNSTILE_ANON_HOURLY_THRESHOLD") or "5"
    )
except ValueError:
    TURNSTILE_ANON_HOURLY_THRESHOLD = 5


def hash_ip(client_ip: str) -> str:
    """Public so callers (e.g. asgi.py's mitigation-logging call site) reuse
    this instead of re-implementing the same truncated-sha256 scheme
    backend/rate_limit.py's _hash_key() already uses for the same purpose --
    never log a raw IP or Clerk sub (plan.md §8.D privacy)."""
    return hashlib.sha256((client_ip or "").encode()).hexdigest()[:16]


async def is_challenge_required(client_ip: str) -> bool:
    """True once *client_ip* has made more than TURNSTILE_ANON_HOURLY_THRESHOLD
    anonymous /ask requests in the trailing hour.

    Uses backend.rate_limit.get_shared_store() (plan.md §3 reuse rule --
    the same cross-instance-safe store the rate limiter and the cost
    breaker already share) under its own key namespace (`ts:hourly:...`),
    so this counter cannot collide with either the per-minute `rl:llm:...`
    buckets or the cost breaker's cache key. Every anonymous /ask call
    increments this counter regardless of whether a challenge ends up being
    required, by design -- it is the count that decides the threshold.

    Fails OPEN on a store error: Turnstile is anti-abuse polish (plan.md
    §16.4 "supporting hardening"), not the budget-protecting llm-class
    check that plan.md §16.3-L2 requires to fail closed -- a Redis blip
    here should not additionally block anonymous /ask on top of whatever
    backend.rate_limit's own llm-class fail-closed posture already decided.
    """
    # Local import: mirrors backend/cost_meter.py's is_global_cost_breaker_tripped()
    # lazy import of the same accessor, breaking the same potential import
    # cycle (backend.rate_limit -> backend.helpers -> backend.utils.search_provider
    # -> backend.claude -> ... ) by deferring the import to call time.
    from backend.rate_limit import get_shared_store

    store = get_shared_store()
    key = f"ts:hourly:ip:{hash_ip(client_ip)}"
    try:
        count = await store.incr(key, 3600)
    except Exception as exc:
        logger.debug("turnstile hourly counter unavailable, failing open: %s", exc)
        return False
    return count > TURNSTILE_ANON_HOURLY_THRESHOLD


async def verify_turnstile_token(token: str, remote_ip: str) -> bool:
    """POST the submitted token to Cloudflare's siteverify endpoint.

    Uses httpx's async client (project rule: never block the FastAPI event
    loop with a synchronous HTTP call -- .agents/ENGINEERING_RULES.md).

    Fails CLOSED (returns False) on a missing token, a missing/misconfigured
    TURNSTILE_SECRET_KEY, or any siteverify request error -- the opposite
    posture from is_challenge_required() above, and deliberately so: that
    function decides whether a captcha is owed at all (anti-abuse polish,
    fail-open is safe), while this one decides whether a *specific proof*
    is valid (the actual gate an attacker would want to bypass by making
    siteverify look unreachable -- fail-closed is the only sound default
    for a verification check, not a rate limit).
    """
    if not TURNSTILE_SECRET_KEY:
        logger.warning(
            "TURNSTILE_ENABLED is true but TURNSTILE_SECRET_KEY is unset -- "
            "every Turnstile-gated request will be rejected until this is "
            "configured."
        )
        return False
    if not token:
        return False

    import httpx  # local import: keeps this dependency optional until the flag is on

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                _SITEVERIFY_URL,
                data={
                    "secret": TURNSTILE_SECRET_KEY,
                    "response": token,
                    "remoteip": remote_ip,
                },
            )
        result = response.json()
    except Exception as exc:
        logger.warning("turnstile siteverify request failed: %s", exc)
        return False

    return bool(isinstance(result, dict) and result.get("success"))


async def enforce_anonymous_ask_gate(client_ip: str, token: str | None) -> bool:
    """Single entry point for asgi.py::ask_async. Returns True if the
    request may proceed.

    True no-op when TURNSTILE_ENABLED is unset -- callers do not need to
    check the flag themselves first, matching this module's docstring
    guarantee that local dev/tests are unaffected by default.
    """
    if not TURNSTILE_ENABLED:
        return True
    if not await is_challenge_required(client_ip):
        return True
    return await verify_turnstile_token(token or "", client_ip)
