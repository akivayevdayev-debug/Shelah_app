"""
Coverage-expansion tests for the remaining small-gap backend modules:
cost_meter.py, data_service.py, customs.py, sefaria.py. Each section targets
the specific uncovered branches (client-None short-circuits, exception
fallbacks, malformed-data skip branches) rather than re-testing already
covered happy paths.
"""

from __future__ import annotations

import asyncio

import pytest

import backend.cost_meter as cost_meter
import backend.data_service as data_service
import backend.customs as customs_module
import backend.sefaria as sefaria_module


# ─────────────────────────── cost_meter.py ─────────────────────────────────

class TestInsertUsageRow:
    def test_no_client_returns_without_error(self, monkeypatch):
        import app
        monkeypatch.setattr(app, "_get_supabase_client", lambda: None)
        cost_meter._insert_usage_row({"provider": "anthropic"})  # should not raise

    def test_client_exception_is_swallowed(self, monkeypatch):
        import app

        class FakeClient:
            def table(self, name):
                raise RuntimeError("db down")

        monkeypatch.setattr(app, "_get_supabase_client", lambda: FakeClient())
        cost_meter._insert_usage_row({"provider": "anthropic"})  # should not raise

    def test_happy_path_inserts_row(self, monkeypatch):
        import app
        inserted = []

        class FakeQuery:
            def insert(self, row):
                inserted.append(row)
                return self

            def execute(self):
                return None

        class FakeClient:
            def table(self, name):
                return FakeQuery()

        monkeypatch.setattr(app, "_get_supabase_client", lambda: FakeClient())
        cost_meter._insert_usage_row({"provider": "anthropic", "model": "x"})
        assert inserted == [{"provider": "anthropic", "model": "x"}]


class TestRecordLlmCall:
    async def test_record_llm_call_completes_without_raising(self, monkeypatch):
        import app
        monkeypatch.setattr(app, "_get_supabase_client", lambda: None)
        await cost_meter.record_llm_call(
            provider="anthropic", model="claude-haiku-4-5",
            input_tokens=100, output_tokens=50, route="/ask",
        )


# ─────────────────────────── data_service.py ───────────────────────────────

class TestShelahEngineGetLibraryText:
    def test_error_response_returns_error_shape(self, monkeypatch):
        engine = data_service.ShelahEngine()
        monkeypatch.setattr(
            data_service, "get_text",
            lambda ref: {"error": "Text not found", "ref": ref, "he": [], "en": []},
        )
        result = engine.get_library_text("Nonexistent Ref")
        assert result["lines"][0]["en"] == "Text not found"

    def test_exception_returns_fallback_shape(self, monkeypatch):
        engine = data_service.ShelahEngine()

        def _raise(ref):
            raise RuntimeError("network down")

        monkeypatch.setattr(data_service, "get_text", _raise)
        result = engine.get_library_text("Genesis 1:1")
        assert result["lines"][0]["en"] == "Failed to fetch source."

    def test_happy_path_returns_ref_and_lines(self, monkeypatch):
        engine = data_service.ShelahEngine()
        monkeypatch.setattr(
            data_service, "get_text",
            lambda ref: {"ref": ref, "lines": [{"he": "x", "en": "y"}], "error": None},
        )
        result = engine.get_library_text("Genesis 1:1")
        assert result["ref"] == "Genesis 1:1"
        assert result["lines"] == [{"he": "x", "en": "y"}]

    def test_get_customs_delegates_to_customs_module(self, monkeypatch):
        engine = data_service.ShelahEngine()
        monkeypatch.setattr(data_service.customs, "search_customs", lambda topic: [{"topic": topic}])
        assert engine.get_customs("shabbat") == [{"topic": "shabbat"}]


# ─────────────────────────── customs.py ────────────────────────────────────

class TestSearchCustomsMalformedData:
    def test_non_dict_topics_value_is_skipped(self, monkeypatch):
        monkeypatch.setattr(
            customs_module, "load_all_customs",
            lambda: {"Ashkenaz": "not a dict, should be skipped"},
        )
        result = customs_module.search_customs("shabbat candles")
        assert result == []

    def test_non_dict_topic_entry_is_skipped(self, monkeypatch):
        monkeypatch.setattr(
            customs_module, "load_all_customs",
            lambda: {"Ashkenaz": {"shabbat": "not a dict"}},
        )
        result = customs_module.search_customs("shabbat candles")
        assert result == []


# ─────────────────────────── sefaria.py ────────────────────────────────────

class TestGetDailyStudyDoubleFailure:
    def test_sefaria_and_calendar_engine_both_fail_returns_offline_payload(self, monkeypatch):
        sefaria_module._DAILY_STUDY_CACHE.clear()

        def _raise_requests(*a, **k):
            raise ConnectionError("sefaria down")

        monkeypatch.setattr(sefaria_module.requests, "get", _raise_requests)

        import backend.calendar_service as calendar_service_module

        def _raise_hebrew():
            raise RuntimeError("calendar engine down")

        monkeypatch.setattr(calendar_service_module.calendar_engine, "gregorian_to_hebrew", _raise_hebrew)
        result = sefaria_module.get_daily_study()
        assert result["offline"] is True
        assert result["hebrew_date"] == ""
        assert result["holiday"] is None
        sefaria_module._DAILY_STUDY_CACHE.clear()


class TestFindRefsForQuestionFallback:
    def test_no_keyword_match_returns_default_refs(self):
        result = sefaria_module.find_refs_for_question("completely unrelated gibberish xyzabc123")
        assert "Shulchan_Arukh,_Orach_Chayim.1" in result
        assert "Rambam,_Mishneh_Torah,_Laws_of_Prayer.1" in result

    def test_max_seven_refs_returned(self):
        result = sefaria_module.find_refs_for_question("shabbat")
        assert len(result) <= 7
