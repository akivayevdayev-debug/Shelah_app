"""
Coverage for the calendar card's clock times: backend/zmanim_engine.get_day_times()
and its route, GET /api/zmanim/days.

The Hebcal month payload below mirrors the real `hebcal?v=1&cfg=json&c=on&maj=on`
shape (only the fields the engine reads): a Friday candle lighting, the Shabbat
havdalah, and a yom tov eve.
"""

from __future__ import annotations

from datetime import date

import pytest

import backend.zmanim_engine as ze
from backend import cache_policy

NYC = (40.7128, -74.0060)
NYC_TZ = "America/New_York"

HEBCAL_SEPTEMBER = {
    "items": [
        {"title": "Candle lighting: 6:44pm", "date": "2026-09-11T18:44:00-04:00", "category": "candles"},
        {"title": "Havdalah: 7:39pm", "date": "2026-09-12T19:39:00-04:00", "category": "havdalah"},
        {"title": "Rosh Hashana 5787", "date": "2026-09-12", "category": "holiday"},
        {"title": "Candle lighting: 6:42pm", "date": "2026-09-11T18:42:00-04:00", "category": "candles",
         "memo": "duplicate stamp for the same day is overwritten, not appended"},
        {"title": "Broken", "date": "2026-09", "category": "candles"},
        {"title": "Garbage", "date": "not-a-date-at-all", "category": "havdalah"},
    ]
}


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _clear_caches():
    ze._HEBCAL_CANDLES_MONTH_CACHE.clear()
    yield
    ze._HEBCAL_CANDLES_MONTH_CACHE.clear()


@pytest.fixture
def hebcal(monkeypatch):
    """Serve HEBCAL_SEPTEMBER and record every Hebcal URL requested."""
    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        return _FakeResponse(HEBCAL_SEPTEMBER)

    monkeypatch.setattr(ze._HTTP, "get", fake_get)
    return calls


class TestMonthCandleTimes:
    def test_keys_candles_and_havdalah_by_day(self, hebcal):
        days = ze._get_hebcal_month_candle_times(*NYC, NYC_TZ, 2026, 9)

        assert days["2026-09-11"]["candles"].hour == 18
        assert days["2026-09-11"]["havdalah"] is None
        assert days["2026-09-12"]["havdalah"].hour == 19
        assert days["2026-09-12"]["candles"] is None

    def test_ignores_non_candle_items_and_unparseable_stamps(self, hebcal):
        days = ze._get_hebcal_month_candle_times(*NYC, NYC_TZ, 2026, 9)

        assert set(days) == {"2026-09-11", "2026-09-12"}

    def test_a_repeat_call_is_served_from_cache(self, hebcal):
        ze._get_hebcal_month_candle_times(*NYC, NYC_TZ, 2026, 9)
        ze._get_hebcal_month_candle_times(*NYC, NYC_TZ, 2026, 9)

        assert len(hebcal) == 1

    def test_the_cache_hands_out_copies(self, hebcal):
        first = ze._get_hebcal_month_candle_times(*NYC, NYC_TZ, 2026, 9)
        first["2026-09-11"]["candles"] = None
        first.clear()

        again = ze._get_hebcal_month_candle_times(*NYC, NYC_TZ, 2026, 9)
        assert again["2026-09-11"]["candles"] is not None

    def test_a_failed_fetch_returns_nothing_and_is_not_cached(self, monkeypatch):
        attempts = []

        def boom(url, timeout=None):
            attempts.append(url)
            raise OSError("hebcal is down")

        monkeypatch.setattr(ze._HTTP, "get", boom)

        assert ze._get_hebcal_month_candle_times(*NYC, NYC_TZ, 2026, 9) == {}
        assert ze._get_hebcal_month_candle_times(*NYC, NYC_TZ, 2026, 9) == {}
        assert len(attempts) == 2

    def test_an_open_circuit_skips_the_network(self, monkeypatch):
        monkeypatch.setattr(ze.health, "is_healthy", lambda name: False)
        monkeypatch.setattr(ze._HTTP, "get", lambda *a, **k: pytest.fail("must not fetch"))

        assert ze._get_hebcal_month_candle_times(*NYC, NYC_TZ, 2026, 9) == {}


class TestGetDayTimes:
    def test_returns_local_iso_times_for_each_day(self, hebcal):
        out = ze.get_day_times(*NYC, [date(2026, 9, 11), date(2026, 9, 12)])

        assert out["timezone"] == NYC_TZ
        friday = out["days"]["2026-09-11"]
        assert friday["candles"].startswith("2026-09-11T18:4")
        assert friday["havdalah"] is None
        assert friday["sunset"].startswith("2026-09-11T19:")
        assert friday["dawn"] < friday["sunset"] < friday["nightfall"]
        assert out["days"]["2026-09-12"]["havdalah"].startswith("2026-09-12T19:39")

    def test_an_ordinary_day_has_solar_times_but_no_candles(self, hebcal):
        day = ze.get_day_times(*NYC, [date(2026, 9, 16)])["days"]["2026-09-16"]

        assert day["candles"] is None and day["havdalah"] is None
        assert day["dawn"] and day["sunset"] and day["nightfall"]

    def test_one_hebcal_fetch_per_distinct_month(self, hebcal):
        ze.get_day_times(*NYC, [date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 2)])

        assert len(hebcal) == 2
        assert "month=9" in hebcal[0] and "month=10" in hebcal[1]

    def test_hebcal_being_down_still_yields_solar_times(self, monkeypatch):
        monkeypatch.setattr(ze._HTTP, "get", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))

        day = ze.get_day_times(*NYC, [date(2026, 9, 11)])["days"]["2026-09-11"]

        assert day["candles"] is None and day["havdalah"] is None
        assert day["sunset"] and day["nightfall"]

    def test_a_day_the_solar_library_rejects_degrades_to_nulls(self, hebcal, monkeypatch):
        def broken(*args, **kwargs):
            raise ValueError("no sunset at this latitude")

        monkeypatch.setattr(ze, "ZmanimCalendar", broken)

        day = ze.get_day_times(*NYC, [date(2026, 9, 11)])["days"]["2026-09-11"]

        assert day["dawn"] is None and day["sunset"] is None and day["nightfall"] is None
        assert day["candles"] is not None  # Hebcal's own stamp survives


class TestDaysRoute:
    URL = "/api/zmanim/days?lat=40.7128&lon=-74.006&dates=2026-09-11,2026-09-12"

    def test_happy_path(self, test_client, hebcal):
        resp = test_client.get(self.URL)

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["timezone"] == NYC_TZ
        assert set(body["days"]) == {"2026-09-11", "2026-09-12"}

    def test_is_public_cacheable_and_never_touches_the_session(self, test_client, hebcal):
        resp = test_client.get(self.URL, headers={"Origin": "http://localhost"})

        assert resp.headers["Cache-Control"] == cache_policy.CACHE_TIER_DATED
        with test_client.session_transaction() as sess:
            assert "lat" not in sess and "lon" not in sess

    @pytest.mark.parametrize("query", [
        "dates=2026-09-11",                                   # no location at all
        "lat=40.7&dates=2026-09-11",                          # half a location
        "lat=40.7&lon=-74&dates=",                            # no dates
        "lat=40.7&lon=-74",                                   # dates missing
        "lat=40.7&lon=-74&dates=2026-09-31",                  # not a real day
        "lat=40.7&lon=-74&dates=tomorrow",                    # not ISO
        "lat=40.7&lon=-74&dates=1500-01-01",                  # out of range
        "lat=999&lon=999&dates=2026-09-11",                   # invalid coordinates
        "lat=40.7&lon=-74&dates=" + ",".join(f"2026-09-{d:02d}" for d in range(1, 18)),  # 17 dates
    ])
    def test_rejects_bad_input(self, test_client, hebcal, query):
        resp = test_client.get(f"/api/zmanim/days?{query}")

        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_duplicate_and_unordered_dates_collapse(self, test_client, hebcal):
        resp = test_client.get("/api/zmanim/days?lat=40.7&lon=-74&dates=2026-09-12,2026-09-11,2026-09-12")

        assert list(resp.get_json()["days"]) == ["2026-09-11", "2026-09-12"]
