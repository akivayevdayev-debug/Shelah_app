"""
Tests for plan.md §20.2 Phase 20b (Prompt 33b) — making the per-user AI
spend ceiling atomic.

Background (plan.md §20.1-C2): the old check_user_budget_and_enforce() read
today's total, compared it to the threshold, and never wrote anything —
cost_meter.py:285-312 (pre-fix). A burst of N concurrent /ask requests from
one caller near the cap could all read the same pre-spend total and all
pass, blowing through PER_USER_DAILY_BUDGET_USD in one shot.

The fix replaces that read with a single atomic Postgres statement
(scripts/sql/check_and_reserve_user_budget.sql) that checks the running
total AND inserts a reservation row in one transaction, serialized per key
via pg_advisory_xact_lock. This file's first test class is the STEP 0
deliverable specified by the prompt: a concurrency test that demonstrably
fails against the old (read-only) implementation and passes against the new
(atomic reserve) one.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from backend import cost_meter, logging_setup


@pytest.fixture(autouse=True)
def _clear_budget_reservation_context():
    logging_setup.bind_budget_reservation("")
    yield
    logging_setup.bind_budget_reservation("")


# ─── STEP 0 — the concurrency deliverable ───────────────────────────────────
#
# These fakes model the SAME two Supabase call shapes the real code uses:
#   - OLD code: client.table("ai_usage_log").select(...).eq(...).gte(...)
#     .execute() — a plain read, exercised by _fetch_today_usage_cost_for_key.
#   - NEW code: client.rpc("check_and_reserve_user_budget", {...}).execute()
#     — exercised by _reserve_budget_or_deny.
# _FakeAtomicBudgetClient answers both, but only the .rpc() path is backed
# by a real threading.Lock (mirroring the SQL function's
# pg_advisory_xact_lock). This is what makes the same test go red against
# the OLD call site and green against the NEW one: the OLD code never sees
# the lock at all.


class _FakeRpcResult:
    def __init__(self, data):
        self.data = data


class _FakeStaleReadQuery:
    """OLD code path: a plain read that always returns the same seeded
    total, no matter how many concurrent reservations have happened via the
    .rpc() path — exactly the bug (a read that never reflects a write)."""

    def __init__(self, total_usd: float):
        self._total_usd = total_usd

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def gte(self, *a, **k):
        return self

    def execute(self):
        return _FakeRpcResult([{"cost_usd": self._total_usd}])


class _FakeAtomicRpcCall:
    def __init__(self, client: "_FakeAtomicBudgetClient", params: dict):
        self._client = client
        self._params = params

    def execute(self):
        allowed, total, reservation_id = self._client._reserve(
            self._params["p_threshold_usd"], self._params["p_reservation_usd"],
        )
        return _FakeRpcResult(
            [{"allowed": allowed, "total_usd": total, "reservation_id": reservation_id}]
        )


class _FakeAtomicBudgetClient:
    """Faithful fake of the atomic RPC's semantics: a real threading.Lock
    serializes check-and-reserve exactly the way the SQL function's
    pg_advisory_xact_lock does. asyncio.to_thread() runs the sync fake in
    real OS threads (the default executor), so this exercises genuine
    concurrency, not just interleaved coroutines."""

    def __init__(self, starting_total_usd: float):
        self._lock = threading.Lock()
        self._total_usd = starting_total_usd
        self.reservation_count = 0

    def table(self, name):
        assert name == "ai_usage_log"
        return _FakeStaleReadQuery(self._total_usd)

    def rpc(self, name, params):
        assert name == "check_and_reserve_user_budget"
        return _FakeAtomicRpcCall(self, params)

    def _reserve(self, threshold_usd: float, reservation_usd: float):
        with self._lock:
            if self._total_usd + reservation_usd > threshold_usd:
                return False, self._total_usd, None
            self._total_usd += reservation_usd
            self.reservation_count += 1
            return True, self._total_usd, f"fake-reservation-{self.reservation_count}"


_CONCURRENCY_THRESHOLD_USD = 2.00
_CONCURRENCY_RESERVATION_USD = 0.002
# Exactly enough headroom under the threshold for ONE reservation
# ($1.997 + $0.002 = $1.999 <= $2.00) but not two ($1.999 + $0.002 =
# $2.001 > $2.00) — proves the atomicity, rather than trivially denying
# everyone.
_CONCURRENCY_STARTING_TOTAL_USD = 1.997


class TestConcurrentBudgetReservationIsAtomic:
    """plan.md §20.2 Phase 20b STEP 0 — the deliverable."""

    async def _run_n_concurrent(self, monkeypatch, n: int, reserve_fn_name: str):
        import app as flask_app_module

        fake_client = _FakeAtomicBudgetClient(_CONCURRENCY_STARTING_TOTAL_USD)
        monkeypatch.setenv(cost_meter._PER_USER_DAILY_BUDGET_ENV, str(_CONCURRENCY_THRESHOLD_USD))
        monkeypatch.setattr(flask_app_module, "_get_supabase_client", lambda: fake_client)
        monkeypatch.setattr(
            cost_meter, "_max_single_ask_reservation_usd", lambda: _CONCURRENCY_RESERVATION_USD,
        )

        results = await asyncio.gather(*[
            cost_meter.check_user_budget_and_enforce("user_racer", "1.2.3.4")
            for _ in range(n)
        ])
        return results

    async def test_new_atomic_implementation_allows_at_most_one(self, monkeypatch):
        """Against the CURRENT (post-fix) check_user_budget_and_enforce(),
        which calls the atomic _reserve_budget_or_deny() RPC wrapper."""
        results = await self._run_n_concurrent(monkeypatch, n=20, reserve_fn_name="rpc")

        allowed_count = sum(1 for r in results if r["allowed"])
        assert allowed_count == 1, (
            f"expected exactly 1 of 20 concurrent callers to be allowed under "
            f"${_CONCURRENCY_RESERVATION_USD} of headroom, got {allowed_count} — "
            "the check-and-reserve is not atomic"
        )

    async def test_old_read_then_decide_implementation_fails_this_test(self, monkeypatch):
        """Demonstrates the defect this fix closes (plan.md §20.1-C2): if
        check_user_budget_and_enforce() is forced back onto the OLD
        read-only call site (_fetch_today_usage_cost_for_key, which only
        ever calls .table(), never .rpc()), the same 20-concurrent-caller
        scenario allows ALL 20 through, because none of them observes any
        of the others' (nonexistent) writes. This is the STEP 0 "confirm it
        FAILS against current code" check, pinned permanently as a
        regression test for the old code path rather than a one-off manual
        run — it asserts the OLD behavior IS broken, so it stays green
        forever and would only go red if _fetch_today_usage_cost_for_key
        somehow became atomic on its own (impossible; it is a plain read)."""
        import app as flask_app_module

        fake_client = _FakeAtomicBudgetClient(_CONCURRENCY_STARTING_TOTAL_USD)
        monkeypatch.setenv(cost_meter._PER_USER_DAILY_BUDGET_ENV, str(_CONCURRENCY_THRESHOLD_USD))
        monkeypatch.setattr(flask_app_module, "_get_supabase_client", lambda: fake_client)

        results = await asyncio.gather(*[
            asyncio.to_thread(
                cost_meter._fetch_today_usage_cost_for_key, "user_id", "user_racer",
            )
            for _ in range(20)
        ])

        # Every single concurrent read sees the same stale $1.997 total —
        # none of them can see a write, because the old code never performs
        # one. All 20 would independently decide `1.997 < 2.00` -> allowed.
        assert all(total == pytest.approx(_CONCURRENCY_STARTING_TOTAL_USD) for total in results)
        assert all(total < _CONCURRENCY_THRESHOLD_USD for total in results), (
            "if this fails, the old read-only helper is somehow no longer "
            "reproducing the race it exists here to document"
        )


# ─── _max_single_ask_reservation_usd ────────────────────────────────────────

def test_max_single_ask_reservation_is_positive_and_small():
    """Sanity bounds: the reservation must be enough to matter (>0) but
    small enough that the $2/day default cap still allows a reasonable
    number of questions per day."""
    reservation = cost_meter._max_single_ask_reservation_usd()
    assert reservation > 0.0
    # Comfortably below the default per-user daily budget, or the ceiling
    # would deny a caller's very first question of the day.
    assert reservation < cost_meter._DEFAULT_PER_USER_DAILY_BUDGET_USD


# ─── _reserve_budget_or_deny ─────────────────────────────────────────────────

def test_reserve_budget_or_deny_returns_allowed_true_when_no_supabase_client(monkeypatch):
    import app as flask_app_module
    monkeypatch.setattr(flask_app_module, "_get_supabase_client", lambda: None)

    result = cost_meter._reserve_budget_or_deny("user_id", "user_123", 2.0, 0.02)

    assert result == {"allowed": True, "total_usd": 0.0, "reservation_id": ""}


def test_reserve_budget_or_deny_returns_allowed_true_for_empty_key(monkeypatch):
    import app as flask_app_module
    monkeypatch.setattr(
        flask_app_module, "_get_supabase_client",
        lambda: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    result = cost_meter._reserve_budget_or_deny("client_key", "", 2.0, 0.02)

    assert result == {"allowed": True, "total_usd": 0.0, "reservation_id": ""}


def test_reserve_budget_or_deny_fails_open_on_transport_error(monkeypatch):
    import app as flask_app_module

    class _BoomClient:
        def rpc(self, name, params):
            raise RuntimeError("function check_and_reserve_user_budget does not exist")

    monkeypatch.setattr(flask_app_module, "_get_supabase_client", lambda: _BoomClient())

    result = cost_meter._reserve_budget_or_deny("user_id", "user_123", 2.0, 0.02)

    assert result == {"allowed": True, "total_usd": 0.0, "reservation_id": ""}


def test_reserve_budget_or_deny_parses_rpc_row_shape(monkeypatch):
    import app as flask_app_module

    class _FakeRpcCall:
        def execute(self):
            return _FakeRpcResult([
                {"allowed": True, "total_usd": 1.23, "reservation_id": "abc-123"},
            ])

    class _FakeClient:
        def rpc(self, name, params):
            assert name == "check_and_reserve_user_budget"
            assert params == {
                "p_key_column": "user_id",
                "p_key_value": "user_123",
                "p_threshold_usd": 2.0,
                "p_reservation_usd": 0.02,
            }
            return _FakeRpcCall()

    monkeypatch.setattr(flask_app_module, "_get_supabase_client", lambda: _FakeClient())

    result = cost_meter._reserve_budget_or_deny("user_id", "user_123", 2.0, 0.02)

    assert result == {"allowed": True, "total_usd": 1.23, "reservation_id": "abc-123"}


def test_reserve_budget_or_deny_denied_has_no_reservation_id(monkeypatch):
    import app as flask_app_module

    class _FakeRpcCall:
        def execute(self):
            return _FakeRpcResult([
                {"allowed": False, "total_usd": 2.00, "reservation_id": None},
            ])

    class _FakeClient:
        def rpc(self, name, params):
            return _FakeRpcCall()

    monkeypatch.setattr(flask_app_module, "_get_supabase_client", lambda: _FakeClient())

    result = cost_meter._reserve_budget_or_deny("user_id", "user_123", 2.0, 0.02)

    assert result == {"allowed": False, "total_usd": 2.00, "reservation_id": ""}


# ─── record_llm_call settling a reservation ─────────────────────────────────

class _FakeUpdateQuery:
    def __init__(self, matched_rows):
        self._matched_rows = matched_rows
        self.updated_with = None
        self.eq_calls = []

    def update(self, values):
        self.updated_with = values
        return self

    def eq(self, column, value):
        self.eq_calls.append((column, value))
        return self

    def execute(self):
        return _FakeRpcResult(self._matched_rows)


class _FakeSettleClient:
    def __init__(self, matched_rows):
        self.query = _FakeUpdateQuery(matched_rows)
        self.inserted_rows = []

    def table(self, name):
        assert name == "ai_usage_log"
        return self.query


async def test_record_llm_call_settles_reservation_instead_of_inserting(monkeypatch):
    import app as flask_app_module

    fake_client = _FakeSettleClient(matched_rows=[{"reservation_id": "resv-9"}])
    monkeypatch.setattr(flask_app_module, "_get_supabase_client", lambda: fake_client)
    insert_calls = []
    monkeypatch.setattr(cost_meter, "_insert_usage_row", lambda row: insert_calls.append(row))

    logging_setup.bind_budget_reservation("resv-9")
    await cost_meter.record_llm_call(
        provider="gemini", model="gemini-3.5-flash-lite",
        input_tokens=100, output_tokens=50, route="/ask",
    )

    assert fake_client.query.eq_calls == [("reservation_id", "resv-9")]
    assert fake_client.query.updated_with["provider"] == "gemini"
    assert fake_client.query.updated_with["reserved"] is False
    assert "created_at" not in fake_client.query.updated_with
    # No second row inserted — the reservation row itself was updated.
    assert insert_calls == []
    # Settling must clear the reservation so a second call in the same
    # request (e.g. a Claude fallback after Gemini) inserts normally.
    assert logging_setup.get_budget_reservation() == ""


async def test_record_llm_call_without_reservation_inserts_normally(monkeypatch):
    insert_calls = []
    monkeypatch.setattr(cost_meter, "_insert_usage_row", lambda row: insert_calls.append(row))
    monkeypatch.setattr(
        cost_meter, "_settle_usage_reservation",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not settle")),
    )

    logging_setup.bind_budget_reservation("")
    await cost_meter.record_llm_call(
        provider="gemini", model="gemini-3.5-flash-lite",
        input_tokens=100, output_tokens=50, route="/ask",
    )

    assert len(insert_calls) == 1


async def test_record_llm_call_second_call_in_same_request_inserts_normally(monkeypatch):
    """First call settles and clears the reservation; a second billed call
    in the same request (Gemini primary + Claude fallback both firing) must
    not try to settle an already-cleared reservation."""
    import app as flask_app_module

    fake_client = _FakeSettleClient(matched_rows=[{"reservation_id": "resv-1"}])
    monkeypatch.setattr(flask_app_module, "_get_supabase_client", lambda: fake_client)
    insert_calls = []
    monkeypatch.setattr(cost_meter, "_insert_usage_row", lambda row: insert_calls.append(row))

    logging_setup.bind_budget_reservation("resv-1")
    await cost_meter.record_llm_call(
        provider="gemini", model="gemini-3.5-flash-lite", input_tokens=10, output_tokens=10,
    )
    await cost_meter.record_llm_call(
        provider="anthropic", model="claude-haiku-4-5", input_tokens=10, output_tokens=10,
    )

    assert len(fake_client.query.eq_calls) == 1  # only one settle happened
    assert len(insert_calls) == 1  # the second call was a normal insert


def test_settle_usage_reservation_falls_back_to_insert_when_reservation_missing(monkeypatch):
    """The reservation row is gone (already TTL-expired and swept) --
    losing it must not lose the underlying spend record."""
    import app as flask_app_module

    fake_client = _FakeSettleClient(matched_rows=[])  # no row matched the .eq() filter
    monkeypatch.setattr(flask_app_module, "_get_supabase_client", lambda: fake_client)
    insert_calls = []
    monkeypatch.setattr(cost_meter, "_insert_usage_row", lambda row: insert_calls.append(row))

    cost_meter._settle_usage_reservation("resv-gone", {"provider": "gemini", "cost_usd": 0.01})

    assert insert_calls == [{"provider": "gemini", "cost_usd": 0.01}]


def test_settle_usage_reservation_reaches_capture_backend_error_on_failure(monkeypatch):
    import app as flask_app_module

    class _BoomClient:
        def table(self, name):
            raise RuntimeError("connection reset")

    monkeypatch.setattr(flask_app_module, "_get_supabase_client", lambda: _BoomClient())
    captured = []
    monkeypatch.setattr(
        cost_meter, "_capture_backend_error",
        lambda event, exc, ctx=None: captured.append((event, exc, ctx)),
    )
    insert_calls = []
    monkeypatch.setattr(cost_meter, "_insert_usage_row", lambda row: insert_calls.append(row))

    cost_meter._settle_usage_reservation("resv-x", {"provider": "gemini", "cost_usd": 0.01})

    assert len(captured) == 1
    assert captured[0][0] == "cost_meter_settle_reservation_failed"
    # Even on a settle failure, the real spend must still be recorded.
    assert insert_calls == [{"provider": "gemini", "cost_usd": 0.01}]


# ─── expire_stale_budget_reservations ───────────────────────────────────────

class _FakeExpireQuery:
    def __init__(self, matched_rows, error=None):
        self._matched_rows = matched_rows
        self._error = error
        self.eq_calls = []
        self.lt_calls = []

    def delete(self):
        return self

    def eq(self, column, value):
        self.eq_calls.append((column, value))
        return self

    def lt(self, column, value):
        self.lt_calls.append((column, value))
        return self

    def execute(self):
        if self._error is not None:
            raise self._error
        return _FakeRpcResult(self._matched_rows)


def test_expire_stale_budget_reservations_deletes_expired_reserved_rows(monkeypatch):
    import app as flask_app_module

    query = _FakeExpireQuery(matched_rows=[{"id": 1}, {"id": 2}])

    class _FakeClient:
        def table(self, name):
            assert name == "ai_usage_log"
            return query

    monkeypatch.setattr(flask_app_module, "_get_supabase_client", lambda: _FakeClient())

    result = cost_meter.expire_stale_budget_reservations()

    assert result == {"deleted": 2}
    assert query.eq_calls == [("reserved", True)]
    assert query.lt_calls[0][0] == "reservation_expires_at"


def test_expire_stale_budget_reservations_no_client_is_a_noop(monkeypatch):
    import app as flask_app_module
    monkeypatch.setattr(flask_app_module, "_get_supabase_client", lambda: None)

    assert cost_meter.expire_stale_budget_reservations() == {"deleted": 0}


def test_expire_stale_budget_reservations_reaches_capture_backend_error_on_failure(monkeypatch):
    import app as flask_app_module

    class _FakeClient:
        def table(self, name):
            return _FakeExpireQuery(matched_rows=[], error=RuntimeError("column does not exist"))

    monkeypatch.setattr(flask_app_module, "_get_supabase_client", lambda: _FakeClient())
    captured = []
    monkeypatch.setattr(
        cost_meter, "_capture_backend_error",
        lambda event, exc, ctx=None: captured.append((event, exc, ctx)),
    )

    result = cost_meter.expire_stale_budget_reservations()

    assert result["deleted"] == 0
    assert "error" in result
    assert len(captured) == 1
    assert captured[0][0] == "budget_reservation_expiry_failed"


# ─── STEP 5 — transport asymmetry: Flask's /ask has no budget check ────────

def test_flask_ask_route_is_unreachable_behind_the_asgi_mount():
    """plan.md §20.2 Phase 20b STEP 5: check_user_budget_and_enforce() has
    exactly one non-test call site, asgi.py's native FastAPI POST /ask.
    Flask's POST /ask (app.py::ask_question) has NO budget check at all —
    defensible only because that route is unreachable in production.

    It's unreachable because asgi.py registers its own "/ask" APIRoute
    BEFORE mounting the Flask app at "/" via WSGIMiddleware, and Starlette's
    router matches routes in registration order — the first match wins, so
    POST /ask always hits the FastAPI route. This test pins that ordering
    rather than leaving the asymmetry as an undocumented assumption (the
    same discipline plan.md §22 applies to the ask_pipeline duplication):
    if a future refactor ever inverted this order, POST /ask requests would
    silently fall through to the Flask route with no spend ceiling at all.
    """
    import asgi

    routes = list(asgi.fastapi_app.router.routes)

    ask_route_index = next(
        i for i, route in enumerate(routes)
        if getattr(route, "path", None) == "/ask"
        and "POST" in (getattr(route, "methods", None) or set())
    )
    flask_mount_index = next(
        i for i, route in enumerate(routes) if type(route).__name__ == "Mount"
    )

    assert ask_route_index < flask_mount_index, (
        "FastAPI's native POST /ask route must be registered before the "
        "WSGIMiddleware Flask mount, or POST /ask requests will silently "
        "reach app.py::ask_question() — which has no "
        "check_user_budget_and_enforce() call."
    )
