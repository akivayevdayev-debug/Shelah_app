"""
Tests for backend/cost_meter.py — LLM token usage / cost tracking.

Covers:
  - estimate_cost_usd(): known-model pricing math, unknown-model fallback,
    zero-token edge cases.
  - record_llm_call(): row shape, extra-dict merge semantics (no overwrite
    of existing keys), and exception-swallowing when the background insert
    fails.

The real Supabase insert path (`_insert_usage_row`) is monkeypatched out so
nothing here touches the network.
"""

from __future__ import annotations

import pytest

from backend import cost_meter


# ─── estimate_cost_usd ─────────────────────────────────────────────────────

def test_estimate_cost_usd_known_model_matches_manual_math():
    model = "claude-sonnet-4-6"
    input_tokens = 10_000
    output_tokens = 2_000
    price = cost_meter._PRICE_PER_M[model]

    expected = (input_tokens * price["input"] + output_tokens * price["output"]) / 1_000_000
    result = cost_meter.estimate_cost_usd(model, input_tokens, output_tokens)

    assert result == pytest.approx(expected)
    # Sanity: also pin the literal value so a future price-table edit is caught.
    assert result == pytest.approx((10_000 * 3.00 + 2_000 * 15.00) / 1_000_000)


def test_estimate_cost_usd_is_case_insensitive():
    """Model lookup lowercases before indexing into _PRICE_PER_M."""
    lower = cost_meter.estimate_cost_usd("claude-haiku-4-5", 1_000, 1_000)
    upper = cost_meter.estimate_cost_usd("CLAUDE-HAIKU-4-5", 1_000, 1_000)
    mixed = cost_meter.estimate_cost_usd("Claude-Haiku-4-5", 1_000, 1_000)

    assert lower == upper == mixed
    assert lower > 0.0


@pytest.mark.parametrize(
    "model",
    ["totally-unknown-model", "gpt-4o", "", "claude-sonnet-4-6-typo"],
)
def test_estimate_cost_usd_unknown_model_falls_back_to_zero(model):
    """Unrecognized model names use _UNKNOWN_PRICE, which is all zeros."""
    result = cost_meter.estimate_cost_usd(model, 50_000, 50_000)
    assert result == 0.0


def test_estimate_cost_usd_zero_tokens_is_zero_cost_even_for_known_model():
    result = cost_meter.estimate_cost_usd("claude-opus-4-8", 0, 0)
    assert result == 0.0


def test_estimate_cost_usd_zero_input_tokens_only_charges_output():
    model = "gemini-1.5-pro"
    price = cost_meter._PRICE_PER_M[model]
    result = cost_meter.estimate_cost_usd(model, 0, 1_000_000)
    assert result == pytest.approx(price["output"])


def test_estimate_cost_usd_zero_output_tokens_only_charges_input():
    model = "gemini-1.5-pro"
    price = cost_meter._PRICE_PER_M[model]
    result = cost_meter.estimate_cost_usd(model, 1_000_000, 0)
    assert result == pytest.approx(price["input"])


# ─── record_llm_call ────────────────────────────────────────────────────────

@pytest.fixture()
def captured_insert(monkeypatch):
    """
    Replace backend.cost_meter._insert_usage_row with a stub that records its
    call args instead of touching Supabase. Returns the list the stub appends
    to, so tests can inspect what would have been inserted.
    """
    calls: list[dict] = []

    def _stub(row):
        calls.append(row)

    monkeypatch.setattr(cost_meter, "_insert_usage_row", _stub)
    return calls


async def test_record_llm_call_builds_expected_row_shape(captured_insert):
    await cost_meter.record_llm_call(
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_tokens=1_000,
        output_tokens=500,
        route="/ask",
        request_id="req-123",
    )

    assert len(captured_insert) == 1
    row = captured_insert[0]

    expected_keys = {
        "provider",
        "model",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "route",
        "request_id",
        "created_at",
    }
    assert expected_keys.issubset(row.keys())

    assert row["provider"] == "anthropic"
    assert row["model"] == "claude-sonnet-4-6"
    assert row["input_tokens"] == 1_000
    assert row["output_tokens"] == 500
    assert row["route"] == "/ask"
    assert row["request_id"] == "req-123"
    assert isinstance(row["cost_usd"], float)
    assert row["cost_usd"] == pytest.approx(
        cost_meter.estimate_cost_usd("claude-sonnet-4-6", 1_000, 500), abs=1e-8
    )
    # created_at is an ISO-ish UTC timestamp string, e.g. 2026-06-16T12:00:00Z
    assert isinstance(row["created_at"], str)
    assert row["created_at"].endswith("Z")


async def test_record_llm_call_defaults_route_and_request_id_to_empty_string(captured_insert):
    await cost_meter.record_llm_call(
        provider="gemini",
        model="gemini-2.0-flash",
        input_tokens=10,
        output_tokens=20,
    )

    row = captured_insert[0]
    assert row["route"] == ""
    assert row["request_id"] == ""


async def test_record_llm_call_merges_extra_keys_without_overwriting_existing(captured_insert):
    """
    Per the exact merge logic in cost_meter.py:
        row.update({k: v for k, v in extra.items() if k not in row})
    Keys already present on the row (provider/model/...) must NOT be
    clobbered by `extra`, but novel keys must be merged in.
    """
    await cost_meter.record_llm_call(
        provider="anthropic",
        model="claude-haiku-4-5",
        input_tokens=100,
        output_tokens=50,
        extra={
            "provider": "should-not-overwrite",  # collides with existing key
            "model": "should-not-overwrite-either",  # collides with existing key
            "session_id": "sess-abc",  # novel key, should be merged in
            "tags": ["foo", "bar"],  # novel key, should be merged in
        },
    )

    row = captured_insert[0]
    # Existing keys must retain their original values.
    assert row["provider"] == "anthropic"
    assert row["model"] == "claude-haiku-4-5"
    # Novel keys from `extra` must be present.
    assert row["session_id"] == "sess-abc"
    assert row["tags"] == ["foo", "bar"]


async def test_record_llm_call_with_no_extra_does_not_raise(captured_insert):
    """extra=None (the default) must be handled gracefully — no `.items()` on None."""
    await cost_meter.record_llm_call(
        provider="anthropic",
        model="claude-3-haiku",
        input_tokens=5,
        output_tokens=5,
    )
    assert len(captured_insert) == 1


async def test_record_llm_call_with_empty_extra_dict_does_not_raise(captured_insert):
    await cost_meter.record_llm_call(
        provider="anthropic",
        model="claude-3-haiku",
        input_tokens=5,
        output_tokens=5,
        extra={},
    )
    assert len(captured_insert) == 1


async def test_record_llm_call_swallows_exception_from_insert_usage_row(monkeypatch):
    """
    If the background insert raises, record_llm_call must not propagate —
    it's a fire-and-forget call wrapped in try/except.
    """

    def _boom(row):
        raise RuntimeError("simulated Supabase failure")

    monkeypatch.setattr(cost_meter, "_insert_usage_row", _boom)

    # Must not raise.
    await cost_meter.record_llm_call(
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_tokens=1,
        output_tokens=1,
    )


async def test_record_llm_call_swallows_exception_with_extra_payload(monkeypatch):
    """Exception-swallowing must also hold when `extra` is provided."""

    def _boom(row):
        raise ValueError("simulated failure with extra")

    monkeypatch.setattr(cost_meter, "_insert_usage_row", _boom)

    await cost_meter.record_llm_call(
        provider="gemini",
        model="gemini-1.5-flash",
        input_tokens=1,
        output_tokens=1,
        extra={"custom_field": "value"},
    )
