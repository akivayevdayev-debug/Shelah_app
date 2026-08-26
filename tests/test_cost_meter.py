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

@pytest.fixture
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


# ─── _daily_budget_usd ──────────────────────────────────────────────────────

def test_daily_budget_usd_unset_returns_zero(monkeypatch):
    monkeypatch.delenv(cost_meter._DAILY_BUDGET_ENV, raising=False)
    assert cost_meter._daily_budget_usd() == 0.0


def test_daily_budget_usd_parses_configured_value(monkeypatch):
    monkeypatch.setenv(cost_meter._DAILY_BUDGET_ENV, "12.5")
    assert cost_meter._daily_budget_usd() == 12.5


def test_daily_budget_usd_invalid_value_returns_zero(monkeypatch):
    monkeypatch.setenv(cost_meter._DAILY_BUDGET_ENV, "not-a-number")
    assert cost_meter._daily_budget_usd() == 0.0


def test_daily_budget_usd_negative_value_clamped_to_zero(monkeypatch):
    monkeypatch.setenv(cost_meter._DAILY_BUDGET_ENV, "-5")
    assert cost_meter._daily_budget_usd() == 0.0


# ─── check_daily_budget_and_alert ───────────────────────────────────────────

@pytest.fixture
def captured_alerts(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        cost_meter, "_capture_backend_error",
        lambda event, error, context=None: calls.append((event, error, context)),
    )
    return calls


async def test_check_daily_budget_disabled_when_unset(monkeypatch, captured_alerts):
    monkeypatch.delenv(cost_meter._DAILY_BUDGET_ENV, raising=False)
    fetch_called = []
    monkeypatch.setattr(
        cost_meter, "_fetch_today_usage_rows",
        lambda: fetch_called.append(1) or [],
    )

    result = await cost_meter.check_daily_budget_and_alert()

    assert result == {
        "configured": False,
        "total_usd": 0.0,
        "threshold_usd": 0.0,
        "call_count": 0,
        "exceeded": False,
    }
    # Disabled means no Supabase read is even attempted.
    assert fetch_called == []
    assert captured_alerts == []


async def test_check_daily_budget_under_threshold_does_not_alert(monkeypatch, captured_alerts):
    monkeypatch.setenv(cost_meter._DAILY_BUDGET_ENV, "10.0")
    monkeypatch.setattr(
        cost_meter, "_fetch_today_usage_rows",
        lambda: [{"cost_usd": 1.0}, {"cost_usd": 2.5}],
    )

    result = await cost_meter.check_daily_budget_and_alert()

    assert result["configured"] is True
    assert result["total_usd"] == pytest.approx(3.5)
    assert result["threshold_usd"] == 10.0
    assert result["call_count"] == 2
    assert result["exceeded"] is False
    assert captured_alerts == []


async def test_check_daily_budget_over_threshold_alerts_once(monkeypatch, captured_alerts):
    monkeypatch.setenv(cost_meter._DAILY_BUDGET_ENV, "5.0")
    monkeypatch.setattr(
        cost_meter, "_fetch_today_usage_rows",
        lambda: [{"cost_usd": 3.0}, {"cost_usd": 4.0}],
    )

    result = await cost_meter.check_daily_budget_and_alert()

    assert result["total_usd"] == pytest.approx(7.0)
    assert result["exceeded"] is True
    assert len(captured_alerts) == 1
    event, error, context = captured_alerts[0]
    assert event == "daily_ai_budget_exceeded"
    assert context["total_usd"] == pytest.approx(7.0)
    assert context["threshold_usd"] == 5.0


async def test_check_daily_budget_exactly_at_threshold_counts_as_exceeded(
    monkeypatch, captured_alerts
):
    monkeypatch.setenv(cost_meter._DAILY_BUDGET_ENV, "5.0")
    monkeypatch.setattr(
        cost_meter, "_fetch_today_usage_rows", lambda: [{"cost_usd": 5.0}],
    )

    result = await cost_meter.check_daily_budget_and_alert()

    assert result["exceeded"] is True
    assert len(captured_alerts) == 1


async def test_check_daily_budget_ignores_non_dict_rows(monkeypatch, captured_alerts):
    monkeypatch.setenv(cost_meter._DAILY_BUDGET_ENV, "5.0")
    monkeypatch.setattr(
        cost_meter, "_fetch_today_usage_rows",
        lambda: [{"cost_usd": 1.0}, "not-a-row", None],
    )

    result = await cost_meter.check_daily_budget_and_alert()

    assert result["total_usd"] == pytest.approx(1.0)
    assert result["exceeded"] is False
    # call_count must track the same valid-row set as total_usd, not
    # len(rows) — a non-dict entry must not inflate the reported call count
    # past what was actually summed.
    assert result["call_count"] == 1


def test_fetch_today_usage_rows_returns_empty_list_when_supabase_unconfigured(monkeypatch):
    import app as flask_app_module
    monkeypatch.setattr(flask_app_module, "_get_supabase_client", lambda: None)
    assert cost_meter._fetch_today_usage_rows() == []


# ─── _fetch_today_usage_rows pagination ─────────────────────────────────────

class _FakePagedResult:
    def __init__(self, data):
        self.data = data


class _FakePagedQuery:
    """Minimal stand-in for the postgrest query-builder chain
    (.select().gte().range().execute()), returning one page of `pages` per
    .execute() call so pagination can be tested without a real Supabase
    client."""

    def __init__(self, pages):
        self._pages = pages
        self._next_page_index = 0
        self.requested_ranges: list[tuple[int, int]] = []

    def select(self, *args, **kwargs):
        return self

    def gte(self, *args, **kwargs):
        return self

    def range(self, start, end):
        self.requested_ranges.append((start, end))
        return self

    def execute(self):
        page = (
            self._pages[self._next_page_index]
            if self._next_page_index < len(self._pages)
            else []
        )
        self._next_page_index += 1
        return _FakePagedResult(page)


class _FakePagedClient:
    def __init__(self, pages):
        self.query = _FakePagedQuery(pages)

    def table(self, name):
        return self.query


def test_fetch_today_usage_rows_stops_after_a_short_page(monkeypatch):
    import app as flask_app_module
    fake_client = _FakePagedClient([[{"cost_usd": 1.0}, {"cost_usd": 2.0}]])
    monkeypatch.setattr(flask_app_module, "_get_supabase_client", lambda: fake_client)

    rows = cost_meter._fetch_today_usage_rows()

    assert len(rows) == 2
    # A single short page means exactly one .range() call was issued.
    assert fake_client.query.requested_ranges == [(0, cost_meter._USAGE_FETCH_PAGE_SIZE - 1)]


def test_fetch_today_usage_rows_paginates_past_a_full_first_page(monkeypatch):
    """Regression test for the undercounting bug: a day with more calls
    than one PostgREST page must not silently truncate."""
    import app as flask_app_module
    page_size = cost_meter._USAGE_FETCH_PAGE_SIZE
    full_page = [{"cost_usd": 0.01} for _ in range(page_size)]
    partial_page = [{"cost_usd": 0.02} for _ in range(3)]
    fake_client = _FakePagedClient([full_page, partial_page])
    monkeypatch.setattr(flask_app_module, "_get_supabase_client", lambda: fake_client)

    rows = cost_meter._fetch_today_usage_rows()

    assert len(rows) == page_size + 3
    assert fake_client.query.requested_ranges == [
        (0, page_size - 1),
        (page_size, 2 * page_size - 1),
    ]


def test_fetch_today_usage_rows_respects_the_max_pages_safety_cap(monkeypatch):
    import app as flask_app_module
    page_size = cost_meter._USAGE_FETCH_PAGE_SIZE
    # Every page is full-size, so pagination would run forever without the cap.
    endless_pages = [
        [{"cost_usd": 0.0} for _ in range(page_size)]
        for _ in range(cost_meter._USAGE_FETCH_MAX_PAGES + 5)
    ]
    fake_client = _FakePagedClient(endless_pages)
    monkeypatch.setattr(flask_app_module, "_get_supabase_client", lambda: fake_client)

    rows = cost_meter._fetch_today_usage_rows()

    assert len(rows) == cost_meter._USAGE_FETCH_MAX_PAGES * page_size


# ─── _per_user_daily_budget_usd ─────────────────────────────────────────────

def test_per_user_daily_budget_usd_unset_uses_default(monkeypatch):
    monkeypatch.delenv(cost_meter._PER_USER_DAILY_BUDGET_ENV, raising=False)
    assert cost_meter._per_user_daily_budget_usd() == cost_meter._DEFAULT_PER_USER_DAILY_BUDGET_USD


def test_per_user_daily_budget_usd_explicit_zero_disables(monkeypatch):
    monkeypatch.setenv(cost_meter._PER_USER_DAILY_BUDGET_ENV, "0")
    assert cost_meter._per_user_daily_budget_usd() == 0.0


def test_per_user_daily_budget_usd_parses_configured_value(monkeypatch):
    monkeypatch.setenv(cost_meter._PER_USER_DAILY_BUDGET_ENV, "5.25")
    assert cost_meter._per_user_daily_budget_usd() == 5.25


def test_per_user_daily_budget_usd_invalid_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(cost_meter._PER_USER_DAILY_BUDGET_ENV, "not-a-number")
    assert cost_meter._per_user_daily_budget_usd() == cost_meter._DEFAULT_PER_USER_DAILY_BUDGET_USD


# ─── check_user_budget_and_enforce ──────────────────────────────────────────
#
# plan.md §20.2 Phase 20b (Prompt 33b) replaced the read-then-decide race
# (_fetch_today_usage_cost_for_key) with an atomic check-and-reserve RPC
# (_reserve_budget_or_deny). These tests were updated to monkeypatch the new
# call site — see tests/test_cost_meter_budget_atomicity.py for the
# concurrency regression test that is the actual deliverable of that fix.

@pytest.fixture(autouse=True)
def _clear_budget_reservation_context():
    """The reservation-id contextvar (backend/logging_setup.py) must not
    leak between tests sharing the same OS thread/context."""
    from backend import logging_setup
    logging_setup.bind_budget_reservation("")
    yield
    logging_setup.bind_budget_reservation("")


async def test_check_user_budget_disabled_when_threshold_zero(monkeypatch):
    monkeypatch.setenv(cost_meter._PER_USER_DAILY_BUDGET_ENV, "0")
    reserve_called = []
    monkeypatch.setattr(
        cost_meter, "_reserve_budget_or_deny",
        lambda col, val, threshold, reservation: reserve_called.append((col, val)) or {
            "allowed": True, "total_usd": 999.0, "reservation_id": "should-not-happen",
        },
    )

    result = await cost_meter.check_user_budget_and_enforce("user_123", "1.2.3.4")

    assert result == {"allowed": True, "total_usd": 0.0, "threshold_usd": 0.0}
    assert reserve_called == []


async def test_check_user_budget_keys_by_user_id_when_present(monkeypatch):
    monkeypatch.setenv(cost_meter._PER_USER_DAILY_BUDGET_ENV, "5.0")
    calls = []
    monkeypatch.setattr(
        cost_meter, "_reserve_budget_or_deny",
        lambda col, val, threshold, reservation: calls.append((col, val)) or {
            "allowed": True, "total_usd": 1.0, "reservation_id": "resv-1",
        },
    )

    result = await cost_meter.check_user_budget_and_enforce("user_123", "1.2.3.4")

    assert calls == [("user_id", "user_123")]
    assert result == {"allowed": True, "total_usd": 1.0, "threshold_usd": 5.0}
    # A successful reservation must be bound into context so record_llm_call()
    # can settle it later.
    assert cost_meter.get_budget_reservation() == "resv-1"


async def test_check_user_budget_falls_back_to_ip_key_when_anonymous(monkeypatch):
    monkeypatch.setenv(cost_meter._PER_USER_DAILY_BUDGET_ENV, "5.0")
    calls = []
    monkeypatch.setattr(
        cost_meter, "_reserve_budget_or_deny",
        lambda col, val, threshold, reservation: calls.append((col, val)) or {
            "allowed": True, "total_usd": 0.0, "reservation_id": "resv-2",
        },
    )

    result = await cost_meter.check_user_budget_and_enforce(None, "1.2.3.4")

    assert calls == [("client_key", "ip:1.2.3.4")]
    assert result["allowed"] is True


async def test_check_user_budget_blocks_when_reservation_denied(monkeypatch):
    monkeypatch.setenv(cost_meter._PER_USER_DAILY_BUDGET_ENV, "5.0")
    monkeypatch.setattr(
        cost_meter, "_reserve_budget_or_deny",
        lambda col, val, threshold, reservation: {
            "allowed": False, "total_usd": 5.0, "reservation_id": "",
        },
    )

    result = await cost_meter.check_user_budget_and_enforce("user_123", "1.2.3.4")

    assert result["allowed"] is False
    assert result["total_usd"] == 5.0
    # A denied call must not bind a reservation for record_llm_call() to settle.
    assert cost_meter.get_budget_reservation() == ""


async def test_check_user_budget_allows_when_no_identity_available(monkeypatch):
    monkeypatch.setenv(cost_meter._PER_USER_DAILY_BUDGET_ENV, "5.0")
    reserve_called = []
    monkeypatch.setattr(
        cost_meter, "_reserve_budget_or_deny",
        lambda col, val, threshold, reservation: reserve_called.append(1) or {
            "allowed": True, "total_usd": 999.0, "reservation_id": "should-not-happen",
        },
    )

    result = await cost_meter.check_user_budget_and_enforce(None, "")

    assert result["allowed"] is True
    assert reserve_called == []


def test_fetch_today_usage_cost_for_key_returns_zero_when_supabase_unconfigured(monkeypatch):
    import app as flask_app_module
    monkeypatch.setattr(flask_app_module, "_get_supabase_client", lambda: None)
    assert cost_meter._fetch_today_usage_cost_for_key("user_id", "user_123") == 0.0


def test_fetch_today_usage_cost_for_key_returns_zero_for_empty_key(monkeypatch):
    import app as flask_app_module
    # A client that would raise if queried -- proves the empty-key guard
    # short-circuits before any Supabase call is made.
    monkeypatch.setattr(
        flask_app_module, "_get_supabase_client",
        lambda: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    assert cost_meter._fetch_today_usage_cost_for_key("client_key", "") == 0.0


class _FakeSumQuery:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    def gte(self, *args, **kwargs):
        return self

    def execute(self):
        return _FakePagedResult(self._rows)


class _FakeSumClient:
    def __init__(self, rows):
        self._query = _FakeSumQuery(rows)

    def table(self, name):
        return self._query


def test_fetch_today_usage_cost_for_key_sums_matching_rows(monkeypatch):
    import app as flask_app_module
    fake_client = _FakeSumClient([{"cost_usd": 1.5}, {"cost_usd": 2.25}])
    monkeypatch.setattr(flask_app_module, "_get_supabase_client", lambda: fake_client)

    total = cost_meter._fetch_today_usage_cost_for_key("user_id", "user_123")

    assert total == pytest.approx(3.75)


def test_fetch_today_usage_cost_for_key_swallows_query_errors(monkeypatch):
    import app as flask_app_module

    class _BoomClient:
        def table(self, name):
            raise RuntimeError("missing column user_id")

    monkeypatch.setattr(flask_app_module, "_get_supabase_client", lambda: _BoomClient())

    assert cost_meter._fetch_today_usage_cost_for_key("user_id", "user_123") == 0.0


# ─── record_llm_call tags caller identity ───────────────────────────────────

async def test_record_llm_call_tags_user_id_from_context(monkeypatch):
    from backend import logging_setup

    captured = {}
    monkeypatch.setattr(cost_meter, "_insert_usage_row", lambda row: captured.update(row))
    logging_setup.bind_user_id("user_abc")
    logging_setup.bind_client_key("")
    try:
        await cost_meter.record_llm_call(
            provider="anthropic", model="claude-haiku-4-5",
            input_tokens=1, output_tokens=1,
        )
    finally:
        logging_setup.bind_user_id("")

    assert captured["user_id"] == "user_abc"
    assert captured["client_key"] == ""


async def test_record_llm_call_tags_client_key_when_anonymous(monkeypatch):
    from backend import logging_setup

    captured = {}
    monkeypatch.setattr(cost_meter, "_insert_usage_row", lambda row: captured.update(row))
    logging_setup.bind_user_id("")
    logging_setup.bind_client_key("ip:9.9.9.9")
    try:
        await cost_meter.record_llm_call(
            provider="anthropic", model="claude-haiku-4-5",
            input_tokens=1, output_tokens=1,
        )
    finally:
        logging_setup.bind_client_key("")

    assert captured["user_id"] == ""
    assert captured["client_key"] == "ip:9.9.9.9"


# ─── is_global_cost_breaker_tripped (plan.md §16.3-L3 / Prompt 29b) ─────────

class _FakeBreakerStore:
    """Minimal get/setex stand-in for backend.rate_limit's shared store --
    avoids depending on _InMemoryStore/_RedisStore internals for these
    tests, which only care about the breaker's own read/compute/write logic."""

    def __init__(self, initial=None, raise_on_get=False, raise_on_setex=False):
        self._values = dict(initial or {})
        self._raise_on_get = raise_on_get
        self._raise_on_setex = raise_on_setex
        self.setex_calls: list[tuple[str, int, str]] = []

    async def get(self, key):
        if self._raise_on_get:
            raise RuntimeError("store unreachable")
        return self._values.get(key)

    async def setex(self, key, ttl_seconds, value):
        if self._raise_on_setex:
            raise RuntimeError("store unreachable")
        self.setex_calls.append((key, ttl_seconds, value))
        self._values[key] = value


@pytest.fixture
def fake_breaker_store(monkeypatch):
    from backend import rate_limit
    store = _FakeBreakerStore()
    monkeypatch.setattr(rate_limit, "get_shared_store", lambda: store)
    return store


async def test_breaker_disabled_when_unset(monkeypatch):
    monkeypatch.delenv(cost_meter._DAILY_BUDGET_ENV, raising=False)
    fetch_called = []
    monkeypatch.setattr(
        cost_meter, "_fetch_today_usage_rows",
        lambda: fetch_called.append(1) or [],
    )

    result = await cost_meter.is_global_cost_breaker_tripped()

    assert result == {
        "tripped": False, "total_usd": 0.0, "threshold_usd": 0.0, "configured": False,
    }
    # Disabled means no Supabase read and no store access is even attempted.
    assert fetch_called == []


async def test_breaker_under_threshold_not_tripped(monkeypatch, fake_breaker_store, captured_alerts):
    monkeypatch.setenv(cost_meter._DAILY_BUDGET_ENV, "10.0")
    monkeypatch.setattr(
        cost_meter, "_fetch_today_usage_rows",
        lambda: [{"cost_usd": 1.0}, {"cost_usd": 2.0}],
    )

    result = await cost_meter.is_global_cost_breaker_tripped()

    assert result["configured"] is True
    assert result["tripped"] is False
    assert result["total_usd"] == pytest.approx(3.0)
    assert captured_alerts == []
    # The freshly computed total is cached for the next call to reuse.
    assert fake_breaker_store.setex_calls == [
        (cost_meter._GLOBAL_BREAKER_CACHE_KEY, cost_meter._GLOBAL_BREAKER_CACHE_TTL_SECONDS, "3.0"),
    ]


async def test_breaker_over_threshold_trips_and_alerts(monkeypatch, fake_breaker_store, captured_alerts):
    monkeypatch.setenv(cost_meter._DAILY_BUDGET_ENV, "5.0")
    monkeypatch.setattr(
        cost_meter, "_fetch_today_usage_rows",
        lambda: [{"cost_usd": 3.0}, {"cost_usd": 4.0}],
    )

    result = await cost_meter.is_global_cost_breaker_tripped()

    assert result["tripped"] is True
    assert result["total_usd"] == pytest.approx(7.0)
    assert len(captured_alerts) == 1
    event, error, context = captured_alerts[0]
    assert event == "global_cost_breaker_tripped"
    assert context["total_usd"] == pytest.approx(7.0)


async def test_breaker_at_exact_threshold_counts_as_tripped(monkeypatch, fake_breaker_store, captured_alerts):
    monkeypatch.setenv(cost_meter._DAILY_BUDGET_ENV, "5.0")
    monkeypatch.setattr(
        cost_meter, "_fetch_today_usage_rows", lambda: [{"cost_usd": 5.0}],
    )

    result = await cost_meter.is_global_cost_breaker_tripped()

    assert result["tripped"] is True
    assert len(captured_alerts) == 1


async def test_breaker_uses_cached_total_without_refetching(monkeypatch, fake_breaker_store):
    monkeypatch.setenv(cost_meter._DAILY_BUDGET_ENV, "5.0")
    fake_breaker_store._values[cost_meter._GLOBAL_BREAKER_CACHE_KEY] = "6.0"
    fetch_called = []
    monkeypatch.setattr(
        cost_meter, "_fetch_today_usage_rows",
        lambda: fetch_called.append(1) or [{"cost_usd": 999.0}],
    )

    result = await cost_meter.is_global_cost_breaker_tripped()

    assert result["tripped"] is True
    assert result["total_usd"] == pytest.approx(6.0)
    # Cache hit -- no Supabase read needed, keeping the hot /ask path cheap.
    assert fetch_called == []


async def test_breaker_ignores_corrupt_cache_value_and_recomputes(monkeypatch, fake_breaker_store):
    monkeypatch.setenv(cost_meter._DAILY_BUDGET_ENV, "5.0")
    fake_breaker_store._values[cost_meter._GLOBAL_BREAKER_CACHE_KEY] = "not-a-number"
    monkeypatch.setattr(
        cost_meter, "_fetch_today_usage_rows",
        lambda: [{"cost_usd": 1.0}],
    )

    result = await cost_meter.is_global_cost_breaker_tripped()

    assert result["total_usd"] == pytest.approx(1.0)


async def test_breaker_fails_open_when_cache_read_errors(monkeypatch):
    from backend import rate_limit
    store = _FakeBreakerStore(raise_on_get=True)
    monkeypatch.setattr(rate_limit, "get_shared_store", lambda: store)
    monkeypatch.setenv(cost_meter._DAILY_BUDGET_ENV, "5.0")
    monkeypatch.setattr(
        cost_meter, "_fetch_today_usage_rows",
        lambda: [{"cost_usd": 1.0}],
    )

    result = await cost_meter.is_global_cost_breaker_tripped()

    # A cache-read failure falls through to a fresh Supabase computation
    # rather than raising -- same fail-open posture as every other
    # Supabase-touching function in this module (_fetch_today_usage_rows,
    # _reserve_budget_or_deny, expire_stale_budget_reservations).
    assert result["configured"] is True
    assert result["total_usd"] == pytest.approx(1.0)


async def test_breaker_fails_open_when_cache_write_errors(monkeypatch):
    from backend import rate_limit
    store = _FakeBreakerStore(raise_on_setex=True)
    monkeypatch.setattr(rate_limit, "get_shared_store", lambda: store)
    monkeypatch.setenv(cost_meter._DAILY_BUDGET_ENV, "5.0")
    monkeypatch.setattr(
        cost_meter, "_fetch_today_usage_rows",
        lambda: [{"cost_usd": 1.0}],
    )

    result = await cost_meter.is_global_cost_breaker_tripped()

    assert result["configured"] is True
    assert result["total_usd"] == pytest.approx(1.0)
