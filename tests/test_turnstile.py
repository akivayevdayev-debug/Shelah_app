"""
Direct unit tests for backend/turnstile.py (plan.md §16.4 / §16.6 Phase 9c).

Integration coverage of the full /ask 403 flow lives in
tests/test_ask.py::TestAskTurnstileGate; this file covers the module's
functions in isolation -- the hourly-threshold counter, siteverify success/
failure/error handling, and the disabled-by-default no-op guarantee.
"""

from __future__ import annotations

import httpx

import backend.turnstile as turnstile_mod


# ─── enforce_anonymous_ask_gate() no-op guarantee ─────────────────────────────

async def test_enforce_gate_is_true_and_a_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(turnstile_mod, "TURNSTILE_ENABLED", False)

    async def _boom(*_args, **_kwargs):
        raise AssertionError("must not be called while TURNSTILE_ENABLED is false")

    monkeypatch.setattr(turnstile_mod, "is_challenge_required", _boom)
    monkeypatch.setattr(turnstile_mod, "verify_turnstile_token", _boom)

    assert await turnstile_mod.enforce_anonymous_ask_gate("203.0.113.50", None) is True


# ─── is_challenge_required() hourly threshold ─────────────────────────────────

async def test_challenge_not_required_until_past_the_hourly_threshold():
    ip = "203.0.113.51"
    for _ in range(turnstile_mod.TURNSTILE_ANON_HOURLY_THRESHOLD):
        assert await turnstile_mod.is_challenge_required(ip) is False
    assert await turnstile_mod.is_challenge_required(ip) is True


async def test_challenge_threshold_is_per_ip():
    below = "203.0.113.52"
    for _ in range(turnstile_mod.TURNSTILE_ANON_HOURLY_THRESHOLD):
        await turnstile_mod.is_challenge_required(below)
    # A different IP's own counter starts fresh regardless of the above.
    fresh = "203.0.113.53"
    assert await turnstile_mod.is_challenge_required(fresh) is False


async def test_challenge_required_fails_open_on_store_error(monkeypatch):
    import backend.rate_limit as rate_limit_mod

    class _BoomStore:
        async def incr(self, *_args, **_kwargs):
            raise rate_limit_mod._StoreUnavailable("boom")

    monkeypatch.setattr(rate_limit_mod, "get_shared_store", lambda: _BoomStore())
    assert await turnstile_mod.is_challenge_required("203.0.113.54") is False


# ─── verify_turnstile_token() ─────────────────────────────────────────────────

async def test_verify_token_missing_token_fails_closed_without_a_network_call(monkeypatch):
    monkeypatch.setattr(turnstile_mod, "TURNSTILE_SECRET_KEY", "test-secret")

    async def _boom(*_args, **_kwargs):
        raise AssertionError("must not attempt siteverify with no token")

    monkeypatch.setattr(httpx, "AsyncClient", _boom)
    assert await turnstile_mod.verify_turnstile_token("", "203.0.113.55") is False


async def test_verify_token_missing_secret_key_fails_closed(monkeypatch):
    monkeypatch.setattr(turnstile_mod, "TURNSTILE_SECRET_KEY", "")
    assert await turnstile_mod.verify_turnstile_token("some-token", "203.0.113.56") is False


async def test_verify_token_success(monkeypatch, mock_outbound_httpx):
    monkeypatch.setattr(turnstile_mod, "TURNSTILE_SECRET_KEY", "test-secret")
    mock_outbound_httpx.post(url__regex=r"https://challenges\.cloudflare\.com/.*").mock(
        return_value=httpx.Response(200, json={"success": True}),
    )
    assert await turnstile_mod.verify_turnstile_token("good-token", "203.0.113.57") is True


async def test_verify_token_rejected_by_cloudflare(monkeypatch, mock_outbound_httpx):
    monkeypatch.setattr(turnstile_mod, "TURNSTILE_SECRET_KEY", "test-secret")
    mock_outbound_httpx.post(url__regex=r"https://challenges\.cloudflare\.com/.*").mock(
        return_value=httpx.Response(200, json={"success": False, "error-codes": ["invalid-input-response"]}),
    )
    assert await turnstile_mod.verify_turnstile_token("bad-token", "203.0.113.58") is False


async def test_verify_token_network_error_fails_closed(monkeypatch, mock_outbound_httpx):
    monkeypatch.setattr(turnstile_mod, "TURNSTILE_SECRET_KEY", "test-secret")
    mock_outbound_httpx.post(url__regex=r"https://challenges\.cloudflare\.com/.*").mock(
        side_effect=httpx.ConnectError("boom"),
    )
    assert await turnstile_mod.verify_turnstile_token("any-token", "203.0.113.59") is False


# ─── enforce_anonymous_ask_gate() end-to-end ──────────────────────────────────

async def test_enforce_gate_below_threshold_never_touches_verification(monkeypatch):
    monkeypatch.setattr(turnstile_mod, "TURNSTILE_ENABLED", True)

    async def _boom(*_args, **_kwargs):
        raise AssertionError("must not verify a token before the threshold is crossed")

    monkeypatch.setattr(turnstile_mod, "verify_turnstile_token", _boom)
    assert await turnstile_mod.enforce_anonymous_ask_gate("203.0.113.60", None) is True


async def test_enforce_gate_past_threshold_requires_a_valid_token(monkeypatch):
    monkeypatch.setattr(turnstile_mod, "TURNSTILE_ENABLED", True)
    ip = "203.0.113.61"
    for _ in range(turnstile_mod.TURNSTILE_ANON_HOURLY_THRESHOLD):
        await turnstile_mod.enforce_anonymous_ask_gate(ip, None)

    monkeypatch.setattr(turnstile_mod, "verify_turnstile_token", lambda *_a, **_k: _async_true())
    assert await turnstile_mod.enforce_anonymous_ask_gate(ip, "some-token") is True


async def _async_true():
    return True


def _load_fresh_turnstile_copy():
    """Execute turnstile.py's module-level code again in an isolated module
    object, so the shared backend.turnstile (imported elsewhere) is untouched."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("turnstile_env_probe", turnstile_mod.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hourly_threshold_defaults_to_five_when_the_env_var_is_unset(monkeypatch):
    monkeypatch.delenv("TURNSTILE_ANON_HOURLY_THRESHOLD", raising=False)

    assert _load_fresh_turnstile_copy().TURNSTILE_ANON_HOURLY_THRESHOLD == 5


def test_hourly_threshold_reads_the_env_var(monkeypatch):
    monkeypatch.setenv("TURNSTILE_ANON_HOURLY_THRESHOLD", "12")

    assert _load_fresh_turnstile_copy().TURNSTILE_ANON_HOURLY_THRESHOLD == 12


def test_a_non_numeric_hourly_threshold_falls_back_to_five(monkeypatch):
    monkeypatch.setenv("TURNSTILE_ANON_HOURLY_THRESHOLD", "many")

    assert _load_fresh_turnstile_copy().TURNSTILE_ANON_HOURLY_THRESHOLD == 5
