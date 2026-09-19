"""
Behavioural coverage for small leftover branches across search.py, helpers.py,
health_check.py, ask_pipeline.py, claude.py, data_service.py, routes_privacy.py,
routes_community.py, utils/search_provider.py and three scripts/ entry points.

Each class targets one module; the docstrings say which behaviour the test pins
so a change to the covered line fails the test rather than merely executing it.

Notes on the two less obvious techniques used here:
  * backend/claude.py's optional ``google.api_core`` import runs once at import
    time and that package is not installed in this environment, so the
    success branch is exercised by executing a private copy of the module file
    with a stand-in ``google.api_core.exceptions`` module registered.
  * The scripts' ``if __name__ == "__main__"`` guards are exercised with
    ``runpy.run_path(..., run_name="__main__")`` so the real file is executed
    (and measured) as a script.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import runpy
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import requests
import responses as responses_lib
from freezegun import freeze_time

import backend.ask_pipeline as ask_pipeline
import backend.data_service as data_service
import backend.health_check as health_check
import backend.helpers as helpers
import backend.routes_community as routes_community_module
import backend.routes_privacy as routes_privacy_module
import backend.search as search_module
from app import app as flask_app
from backend.utils import search_provider as sp

REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_PATH = REPO_ROOT / "backend" / "claude.py"
MERGE_LCOV_PATH = REPO_ROOT / "scripts" / "merge_lcov.py"
VERIFY_INTEGRATIONS_PATH = REPO_ROOT / "scripts" / "verify_integrations.py"
PROMPT_DOC_SYNC_PATH = REPO_ROOT / "scripts" / "check_prompt_doc_sync.py"

HALACHIPEDIA_URL_RE = re.compile(r"https://halachipedia\.com/api\.php.*")
HEBREWBOOKS_URL_RE = re.compile(r"https://www\.hebrewbooks\.org/search\.aspx.*")


# ─────────────────────────────── backend/search.py ───────────────────────────


@pytest.fixture
def clean_search_caches():
    caches = (
        search_module._WIKI_CACHE,
        search_module._HALACHIPEDIA_CACHE,
        search_module._HEBREWBOOKS_CACHE,
    )
    for cache in caches:
        cache.clear()
    yield
    for cache in caches:
        cache.clear()


@pytest.mark.usefixtures("clean_search_caches")
class TestSearchHalachipediaSync:
    def test_cached_payload_is_returned_without_any_request(self, mock_outbound_http):
        payload = {"title": "[Halachipedia] Cached", "summary": "from cache"}
        search_module._HALACHIPEDIA_CACHE.set("shabbat candles", payload)

        # Key is the stripped, lower-cased query.
        assert search_module.search_halachipedia("  Shabbat Candles ") == payload
        assert len(mock_outbound_http.calls) == 0

    def test_article_search_hit_with_no_pages_returns_none_and_is_not_cached(
        self, mock_outbound_http
    ):
        mock_outbound_http.add(
            responses_lib.GET, HALACHIPEDIA_URL_RE,
            json={"query": {"search": [{"title": "Niddah"}]}}, status=200,
        )
        mock_outbound_http.add(
            responses_lib.GET, HALACHIPEDIA_URL_RE,
            json={"query": {"pages": {}}}, status=200,
        )

        assert search_module.search_halachipedia("niddah") is None
        assert search_module._HALACHIPEDIA_CACHE.get("niddah") is None
        # Both the title search and the extract fetch were attempted.
        assert len(mock_outbound_http.calls) == 2


@pytest.mark.usefixtures("clean_search_caches")
class TestAsyncHalachipedia:
    async def test_cached_payload_is_returned_without_any_request(self, mock_outbound_httpx):
        route = mock_outbound_httpx.get(HALACHIPEDIA_URL_RE).mock(
            return_value=httpx.Response(500)
        )
        payload = {"title": "[Halachipedia] Cached", "summary": "from cache"}
        search_module._HALACHIPEDIA_CACHE.set("kashrut", payload)

        assert await search_module.async_search_halachipedia(" KASHRUT ") == payload
        assert route.call_count == 0

    async def test_search_hit_with_blank_title_stops_before_the_extract_request(
        self, mock_outbound_httpx
    ):
        route = mock_outbound_httpx.get(HALACHIPEDIA_URL_RE).mock(
            return_value=httpx.Response(200, json={"query": {"search": [{"title": ""}]}})
        )

        assert await search_module.async_search_halachipedia("blank title hit") is None
        assert route.call_count == 1
        assert search_module._HALACHIPEDIA_CACHE.get("blank title hit") is None

    async def test_search_hit_without_a_title_key_stops_before_the_extract_request(
        self, mock_outbound_httpx
    ):
        route = mock_outbound_httpx.get(HALACHIPEDIA_URL_RE).mock(
            return_value=httpx.Response(200, json={"query": {"search": [{"pageid": 7}]}})
        )

        assert await search_module.async_search_halachipedia("no title key") is None
        assert route.call_count == 1

    async def test_extract_without_pages_returns_none_and_is_not_cached(
        self, mock_outbound_httpx
    ):
        route = mock_outbound_httpx.get(HALACHIPEDIA_URL_RE).mock(side_effect=[
            httpx.Response(200, json={"query": {"search": [{"title": "Niddah"}]}}),
            httpx.Response(200, json={"query": {"pages": {}}}),
        ])

        assert await search_module.async_search_halachipedia("niddah pages") is None
        assert route.call_count == 2
        assert search_module._HALACHIPEDIA_CACHE.get("niddah pages") is None


@pytest.mark.usefixtures("clean_search_caches")
class TestAsyncHebrewBooksCache:
    async def test_cached_payload_is_returned_without_any_request(self, mock_outbound_httpx):
        route = mock_outbound_httpx.get(HEBREWBOOKS_URL_RE).mock(
            return_value=httpx.Response(500)
        )
        payload = {"title": "[HebrewBooks] Cached", "summary": "from cache"}
        search_module._HEBREWBOOKS_CACHE.set("shulchan arukh", payload)

        assert await search_module.async_search_hebrewbooks(" Shulchan Arukh ") == payload
        assert route.call_count == 0


# ─────────────────────────────── backend/helpers.py ──────────────────────────


class TestIsSameOriginRequest:
    def test_unparseable_origin_header_is_rejected(self):
        # urlparse() raises ValueError("Invalid IPv6 URL") for an unclosed bracket.
        with flask_app.test_request_context(
            "/", base_url="http://shelah.test", headers={"Origin": "http://[::1"}
        ):
            assert helpers._is_same_origin_request() is False

    def test_unparseable_referer_fallback_is_rejected_too(self):
        with flask_app.test_request_context(
            "/", base_url="http://shelah.test", headers={"Referer": "http://[::1/page"}
        ):
            assert helpers._is_same_origin_request() is False

    def test_origin_matching_the_request_host_is_accepted(self):
        with flask_app.test_request_context(
            "/", base_url="http://shelah.test", headers={"Origin": "http://shelah.test"}
        ):
            assert helpers._is_same_origin_request() is True

    def test_origin_for_another_host_is_rejected(self):
        with flask_app.test_request_context(
            "/", base_url="http://shelah.test", headers={"Origin": "http://evil.test"}
        ):
            assert helpers._is_same_origin_request() is False


class TestStripCommonHebrewPrefix:
    def test_strips_a_leading_prefix_letter_from_a_long_word(self):
        assert helpers._strip_common_hebrew_prefix("ושמר") == "שמר"

    def test_leaves_a_long_word_without_a_prefix_letter_unchanged(self):
        assert helpers._strip_common_hebrew_prefix("אבגד") == "אבגד"

    def test_leaves_a_short_word_unchanged_even_with_a_prefix_letter(self):
        assert helpers._strip_common_hebrew_prefix("ושם") == "ושם"


class TestCollectWordMeaningAlternativesBlankCandidates:
    def test_a_meaning_that_is_only_punctuation_yields_no_options(self):
        assert helpers._collect_word_meaning_alternatives("word", " . ", False) == []

    def test_blank_chunks_are_skipped_but_real_ones_are_kept(self):
        # "fast; ." splits into "fast" and "." -- the latter strips to "".
        result = helpers._collect_word_meaning_alternatives("word", "fast; .", False)
        assert result == ["fast"]

    def test_hebrew_word_with_nothing_found_online_yields_no_options(self, monkeypatch):
        monkeypatch.setattr(helpers, "HEBREW_INTERPRETIVE_GLOSSARY", {})
        monkeypatch.setattr(helpers, "_lookup_sefaria_lexicon", lambda variant: ("", ""))
        translated_variants = []

        def _no_translation(text, try_fallback=True):
            translated_variants.append(text)
            return "", ""

        monkeypatch.setattr(helpers, "_translate_hebrew_text_online", _no_translation)

        assert helpers._collect_word_meaning_alternatives("שבת", "", True) == []
        # The online translation was actually consulted for the word.
        assert translated_variants == ["שבת"]


class TestTranslateLineIfMissingEnglish:
    def test_line_stays_untouched_when_the_translator_returns_nothing(self, monkeypatch):
        calls = []

        def _empty(text, try_fallback=True):
            calls.append((text, try_fallback))
            return "", "google"

        monkeypatch.setattr(helpers, "_translate_hebrew_text_online", _empty)
        line = {"he": "שלום עולם", "en": ""}

        assert helpers._translate_line_if_missing_english(line) is None
        assert line == {"he": "שלום עולם", "en": ""}
        assert calls == [("שלום עולם", False)]

    def test_line_is_filled_and_source_reported_when_translation_succeeds(self, monkeypatch):
        monkeypatch.setattr(
            helpers, "_translate_hebrew_text_online",
            lambda text, try_fallback=True: ("peace", "google"),
        )
        line = {"he": "שלום", "en": ""}

        assert helpers._translate_line_if_missing_english(line) == "google"
        assert line["en"] == "peace"


class TestTranslateMissingEnglishLinesTimeBudget:
    @staticmethod
    def _clock(monkeypatch, readings):
        remaining = list(readings)

        def _time():
            return remaining.pop(0) if len(remaining) > 1 else remaining[0]

        monkeypatch.setattr(helpers, "time", types.SimpleNamespace(time=_time))

    def test_nothing_is_translated_once_the_budget_is_already_spent(self, monkeypatch):
        translated = []
        monkeypatch.setattr(
            helpers, "_translate_hebrew_text_online",
            lambda text, try_fallback=True: translated.append(text) or ("x", "google"),
        )
        # started_at = 100.0, first per-line check reads 200.0 (> 1.2s budget).
        self._clock(monkeypatch, [100.0, 200.0])
        lines = [{"he": "שלום", "en": ""}, {"he": "עולם", "en": ""}]

        count, sources = helpers._translate_missing_english_lines(
            lines, max_lines=5, max_runtime_seconds=1.2)

        assert (count, sources) == (0, set())
        assert translated == []
        assert [line["en"] for line in lines] == ["", ""]

    def test_loop_stops_mid_way_when_the_budget_runs_out_after_a_translation(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            helpers, "_translate_hebrew_text_online",
            lambda text, try_fallback=True: (f"tr:{text}", "google"),
        )
        # start=100.0, line 1 check=100.5 (in budget), line 2 check=102.0 (over 1.2s).
        self._clock(monkeypatch, [100.0, 100.5, 102.0])
        lines = [{"he": "שלום", "en": ""}, {"he": "עולם", "en": ""}]

        count, sources = helpers._translate_missing_english_lines(
            lines, max_lines=5, max_runtime_seconds=1.2)

        assert count == 1
        assert sources == {"google"}
        assert lines[0]["en"] == "tr:שלום"
        assert lines[1]["en"] == ""


# ───────────────────────────── backend/health_check.py ───────────────────────


class TestCircuitStateShouldProbe:
    def test_healthy_circuit_reports_it_can_be_used(self):
        assert health_check._CircuitState(status="up").should_probe() is True

    def test_half_open_circuit_reports_it_can_be_used(self):
        assert health_check._CircuitState(status="half-open").should_probe() is True

    @freeze_time("2026-01-01 00:00:00")
    def test_open_circuit_waits_for_the_recovery_interval(self):
        now = health_check.time.time()
        just_failed = health_check._CircuitState(status="down", last_failure_ts=now)
        almost = health_check._CircuitState(
            status="down", last_failure_ts=now - (health_check.RECOVERY_INTERVAL - 1))
        elapsed = health_check._CircuitState(
            status="down", last_failure_ts=now - health_check.RECOVERY_INTERVAL)

        assert just_failed.should_probe() is False
        assert almost.should_probe() is False
        assert elapsed.should_probe() is True


class TestProbeWithoutACircuit:
    @pytest.fixture
    def health(self):
        instance = health_check.APIHealth()
        del instance._circuits["sefaria"]
        return instance

    def test_successful_probe_result_is_returned_and_no_circuit_is_created(self, health):
        health._PROBES = {"sefaria": lambda: True}

        assert health._probe("sefaria") is True
        assert "sefaria" not in health._circuits

    def test_failed_probe_result_is_returned_and_no_circuit_is_created(self, health):
        health._PROBES = {"sefaria": lambda: False}

        assert health._probe("sefaria") is False
        assert "sefaria" not in health._circuits

    def test_raising_probe_is_logged_and_reported_as_down(self, health, caplog):
        def _boom():
            raise RuntimeError("boom")

        health._PROBES = {"sefaria": _boom}
        with caplog.at_level("WARNING", logger="backend.health_check"):
            assert health._probe("sefaria") is False

        assert "health_check[sefaria] probe error: boom" in caplog.text
        assert "sefaria" not in health._circuits


# ───────────────────────────── backend/ask_pipeline.py ───────────────────────


class TestToolResultIsInsufficient:
    @pytest.mark.parametrize("result", [None, "text", ["a"], 3])
    def test_non_dict_result_is_insufficient(self, result):
        assert ask_pipeline._tool_result_is_insufficient(result) is True

    def test_error_result_is_insufficient(self):
        assert ask_pipeline._tool_result_is_insufficient({"error": "boom"}) is True

    def test_error_wins_even_when_results_are_present(self):
        result = {"error": "partial", "results": [{"ref": "x"}]}
        assert ask_pipeline._tool_result_is_insufficient(result) is True

    def test_empty_results_list_is_insufficient(self):
        assert ask_pipeline._tool_result_is_insufficient({"results": []}) is True

    def test_non_empty_results_list_is_sufficient(self):
        assert ask_pipeline._tool_result_is_insufficient({"results": [{"ref": "x"}]}) is False

    def test_dict_without_results_or_error_is_sufficient(self):
        assert ask_pipeline._tool_result_is_insufficient({"query": "shabbat"}) is False


# ─────────────────────────────── backend/claude.py ───────────────────────────


def _load_private_claude_copy(monkeypatch, exceptions_module):
    """Execute backend/claude.py's file as a throwaway module so its import-time
    ``google.api_core.exceptions`` lookup runs against the stand-in module."""
    spec = importlib.util.spec_from_file_location("backend_claude_private_copy", CLAUDE_PATH)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    monkeypatch.setitem(sys.modules, "google.api_core.exceptions", exceptions_module)
    # claude.py calls load_dotenv(override=True) at import; never let a re-run
    # clobber this process's environment with a developer's real .env.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    spec.loader.exec_module(module)
    return module


class TestClaudeResourceExhaustedImport:
    def test_module_uses_the_google_api_core_exception_class_when_available(self, monkeypatch):
        fake = types.ModuleType("google.api_core.exceptions")
        fake.ResourceExhausted = type("ResourceExhausted", (Exception,), {})

        module = _load_private_claude_copy(monkeypatch, fake)

        assert module.ResourceExhausted is fake.ResourceExhausted

    def test_module_falls_back_to_exception_when_the_class_is_missing(self, monkeypatch):
        module = _load_private_claude_copy(
            monkeypatch, types.ModuleType("google.api_core.exceptions"))

        assert module.ResourceExhausted is Exception

    def test_gemini_retry_fires_only_for_the_imported_exception_class(self, monkeypatch):
        exhausted = type("ResourceExhausted", (Exception,), {})
        fake = types.ModuleType("google.api_core.exceptions")
        fake.ResourceExhausted = exhausted
        module = _load_private_claude_copy(monkeypatch, fake)
        retrying = module._generate_gemini_content_with_retry.retry
        monkeypatch.setattr(retrying, "sleep", lambda seconds: None)

        flaky = MagicMock()
        flaky.models.generate_content.side_effect = [exhausted("429"), exhausted("429"), "ok"]
        assert module._generate_gemini_content_with_retry(flaky, "model", "prompt") == "ok"
        assert flaky.models.generate_content.call_count == 3

        broken = MagicMock()
        broken.models.generate_content.side_effect = ValueError("bad request")
        with pytest.raises(ValueError):
            module._generate_gemini_content_with_retry(broken, "model", "prompt")
        assert broken.models.generate_content.call_count == 1


# ─────────────────────────────── backend/data_service.py ─────────────────────


class TestShelahEngineDailyLearning:
    def test_delegates_to_sefaria_daily_study_and_returns_its_result(self, monkeypatch):
        calls = []
        sentinel = {"parasha": "Bereshit", "daf": "Berakhot 2a"}

        def _daily_study(*args, **kwargs):
            calls.append((args, kwargs))
            return sentinel

        monkeypatch.setattr(data_service.sefaria, "get_daily_study", _daily_study)

        assert data_service.ShelahEngine().get_daily_learning() is sentinel
        assert calls == [((), {})]


# ───────────────────────────── backend/routes_privacy.py ─────────────────────


class TestRetentionEnforceReservationSweep:
    CRON_HEADERS = {"Authorization": "Bearer top-secret-cron-value"}

    @pytest.fixture
    def supabase(self, monkeypatch):
        monkeypatch.setenv("CRON_SECRET", "top-secret-cron-value")
        client = MagicMock()
        client.table.return_value.delete.return_value.lt.return_value.execute.return_value.data = [
            {"id": "old"}
        ]
        monkeypatch.setattr(routes_privacy_module, "_get_supabase_client", lambda: client)
        return client

    def test_reservation_sweep_error_marks_the_run_failed_with_500(
        self, test_client, monkeypatch, supabase
    ):
        monkeypatch.setattr(
            routes_privacy_module, "expire_stale_budget_reservations",
            lambda: {"error": "rpc unavailable"},
        )

        response = test_client.get("/api/devtools/retention-enforce", headers=self.CRON_HEADERS)

        assert response.status_code == 500
        body = response.get_json()
        assert body["ok"] is False
        assert body["budget_reservations"] == {"error": "rpc unavailable"}
        # The two table purges themselves succeeded before the sweep failed.
        assert body["ask_history"]["deleted"] == 1
        assert body["ai_usage_log"]["deleted"] == 1

    def test_clean_reservation_sweep_keeps_the_run_successful(
        self, test_client, monkeypatch, supabase
    ):
        monkeypatch.setattr(
            routes_privacy_module, "expire_stale_budget_reservations",
            lambda: {"expired": 3},
        )

        response = test_client.get("/api/devtools/retention-enforce", headers=self.CRON_HEADERS)

        assert response.status_code == 200
        body = response.get_json()
        assert body["ok"] is True
        assert body["budget_reservations"] == {"expired": 3}


# ───────────────────────────── backend/routes_community.py ───────────────────


class TestCommunityDetailSkipsNonDictHalachaEntries:
    def test_stray_non_dict_entries_are_ignored_not_fatal(
        self, test_client, monkeypatch, tmp_path
    ):
        filename = routes_community_module.COMMUNITIES["Ashkenaz"]
        customs_dir = tmp_path / "customs"
        customs_dir.mkdir()
        data = {
            "heritage_id": "test-heritage",
            "identity": {"primary_origin": "Testland"},
            "halacha_index": [
                "stray string",
                42,
                None,
                ["nested", "list"],
                {
                    "topic": "Kaddish",
                    "category": "Prayer",
                    "summary": "Stand for it.",
                    "common_practices": ["stand"],
                    "source": "Test Source 1",
                },
            ],
        }
        (customs_dir / f"{filename}.json").write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setattr(routes_community_module, "_PROJECT_ROOT", str(tmp_path))

        response = test_client.get("/api/community/Ashkenaz")

        assert response.status_code == 200
        body = response.get_json()
        assert body["name"] == "Ashkenaz"
        assert body["heritage_id"] == "test-heritage"
        assert body["primary_origin"] == "Testland"
        assert body["customs"] == {
            "prayer_kaddish": {
                "category": "prayer",
                "topic": "kaddish",
                "ruling": "Stand for it.",
                "common_practices": ["stand"],
                "source": "Test Source 1",
            }
        }
        assert body["raw_data"] == data


# ────────────────────────────── backend/utils/search_provider.py ─────────────


class TestBuildDiscoveryQueriesWhitespaceQuestion:
    def test_whitespace_only_question_falls_back_to_itself_as_the_specific_query(self):
        # The question is truthy so it is appended, but the dedupe step drops it
        # as blank; the fallback then restores the raw question.
        result = sp._build_discovery_queries("  \t ", [])

        assert result["specific_queries"] == ["  \t "]
        assert result["broad_queries"] == []
        assert result["topics"] == []


class TestGetHalakhicSourcesLadder:
    @staticmethod
    def _stub_collectors(monkeypatch, sefaria_by_stage, external_by_stage):
        calls = []

        def _sefaria(queries, fallback_terms, discovery_stage, priority, max_results=10):
            calls.append(("sefaria", discovery_stage, priority, max_results, list(fallback_terms)))
            return list(sefaria_by_stage.get(discovery_stage, []))

        def _external(queries, keywords, discovery_stage, priority, max_results=6):
            calls.append(("external", discovery_stage, priority, max_results, list(keywords)))
            return list(external_by_stage.get(discovery_stage, []))

        monkeypatch.setattr(sp, "_collect_global_sefaria_sources", _sefaria)
        monkeypatch.setattr(sp, "_collect_external_global_sources", _external)
        return calls

    def test_question_with_only_stopwords_uses_itself_as_the_keyword(self, monkeypatch):
        calls = self._stub_collectors(monkeypatch, {}, {})

        result = sp.get_halakhic_sources("please tell me")

        assert result["keywords"] == ["please tell me"]
        assert result["status"] == "internal-ai-needed"
        # Every collector call (both stages) received that fallback keyword.
        assert calls
        assert all(call[4] == ["please tell me"] for call in calls)

    def test_broad_stage_sources_are_returned_when_the_specific_stage_finds_nothing(
        self, monkeypatch
    ):
        broad_sefaria = {"ref": "Mishneh Torah, Shabbat 5:1", "corpus": "sefaria-global-search"}
        broad_external = {"ref": "Halachipedia Shabbat", "corpus": "external"}
        calls = self._stub_collectors(
            monkeypatch,
            {"broad-api": [broad_sefaria]},
            {"broad-api": [broad_external]},
        )

        result = sp.get_halakhic_sources("Shabbat candle lighting")

        assert result["status"] == "fallback"
        assert result["fallback_level"] == "broad-api"
        assert result["sources"] == [broad_sefaria, broad_external]
        assert result["source_count"] == 2
        assert result["counts"] == {
            "specific_api": 0, "broad_api": 2, "internal_ai": 0, "sefaria": 1, "external": 1,
        }
        assert result["warning"] == ""
        assert result["internal_disclaimer"] == ""
        assert result["sequence"] == ["specific-api", "broad-api", "internal-ai-knowledge"]
        # Specific stage was tried first, then the broad stage, with the
        # documented priorities / result caps.
        assert [c[:4] for c in calls] == [
            ("sefaria", "specific-api", 1, 8),
            ("external", "specific-api", 1, 4),
            ("sefaria", "broad-api", 2, 10),
            ("external", "broad-api", 2, 6),
        ]

    def test_specific_stage_sources_short_circuit_the_broad_stage(self, monkeypatch):
        specific = {"ref": "Shulchan Arukh, Orach Chayim 242"}
        calls = self._stub_collectors(monkeypatch, {"specific-api": [specific]}, {})

        result = sp.get_halakhic_sources("Shabbat candle lighting")

        assert result["fallback_level"] == "specific-api"
        assert result["sources"] == [specific]
        assert all(call[1] == "specific-api" for call in calls)


# ───────────────────────────────── scripts/ entry points ─────────────────────


def _run_script_as_main(path, argv, monkeypatch):
    monkeypatch.setattr(sys, "argv", [str(path), *argv])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(path), run_name="__main__")
    return excinfo.value.code


class TestMergeLcovScriptEntryPoint:
    RECORDS = (
        "SF:a.js\nDA:1,1\nDA:2,0\nend_of_record\n"
        "SF:a.js\nDA:1,2\nDA:2,0\nend_of_record\n"
    )

    def test_running_as_a_script_merges_stdin_and_exits_zero(self, monkeypatch, capsysbinary):
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(self.RECORDS.encode())))

        code = _run_script_as_main(MERGE_LCOV_PATH, [], monkeypatch)

        captured = capsysbinary.readouterr()
        assert code == 0
        out = captured.out.decode("utf-8")
        assert out.count("SF:a.js") == 1
        assert "DA:1,3" in out
        assert captured.err == b""

    def test_running_as_a_script_with_arguments_exits_with_usage_status(
        self, monkeypatch, capsysbinary
    ):
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))

        code = _run_script_as_main(MERGE_LCOV_PATH, ["in.info"], monkeypatch)

        captured = capsysbinary.readouterr()
        assert code == 2
        assert captured.out == b""
        assert b"Usage: merge_lcov.py < INPUT.info > OUTPUT.info" in captured.err


def _load_verify_integrations(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "verify_integrations_cov_copy", VERIFY_INTEGRATIONS_PATH)
    module = importlib.util.module_from_spec(spec)
    # The script calls load_dotenv() at import time; keep a developer's real
    # .env out of this process.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr(sys, "path", list(sys.path))  # script prepends the repo root
    spec.loader.exec_module(module)
    return module


class TestVerifyIntegrationsScript:
    def test_check_json_files_reports_an_unreadable_file_as_a_failure(
        self, monkeypatch, tmp_path, capsys
    ):
        vi = _load_verify_integrations(monkeypatch)
        customs = tmp_path / "customs"
        customs.mkdir()
        (customs / "good.json").write_text(json.dumps({"halacha_index": [{}]}), encoding="utf-8")
        # A directory named *.json matches the glob but cannot be opened as a
        # file: an error that is not a json.JSONDecodeError.
        (customs / "unreadable.json").mkdir()
        monkeypatch.setattr(vi, "__file__", str(tmp_path / "scripts" / "verify_integrations.py"))

        assert vi.check_json_files() is False

        out = capsys.readouterr().out
        assert "unreadable.json: Error - " in out
        assert "unreadable.json: Invalid JSON" not in out
        # The bad entry did not stop the remaining files from being checked.
        assert "good.json: Valid (1 halacha items)" in out

    def test_check_json_files_error_output_is_truncated_to_fifty_chars(
        self, monkeypatch, tmp_path, capsys
    ):
        vi = _load_verify_integrations(monkeypatch)
        customs = tmp_path / "customs"
        customs.mkdir()
        (customs / "x.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(vi, "__file__", str(tmp_path / "scripts" / "verify_integrations.py"))

        def _explode(*args, **kwargs):
            raise OSError("E" * 200)

        monkeypatch.setattr(vi, "open", _explode, raising=False)

        assert vi.check_json_files() is False

        line = next(
            ln for ln in capsys.readouterr().out.splitlines() if "x.json: Error - " in ln)
        assert line.endswith("x.json: Error - " + "E" * 50)

    def test_running_as_a_script_exits_with_the_status_main_returns(
        self, monkeypatch, capsys
    ):
        monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
        monkeypatch.setattr(sys, "path", list(sys.path))
        # No Supabase config -> that check fails before any network call, and
        # every requests.get() below fails as if offline.
        monkeypatch.delenv("SUPABASE_URL", raising=False)

        def _offline(*args, **kwargs):
            raise requests.ConnectionError("offline")

        monkeypatch.setattr(requests, "get", _offline)

        code = _run_script_as_main(VERIFY_INTEGRATIONS_PATH, [], monkeypatch)

        out = capsys.readouterr().out
        assert code == 1
        assert "Sefaria error: offline" in out
        assert "Some checks failed" in out


class TestCheckPromptDocSyncScriptEntryPoint:
    PLAN = "## 5. Some section\n\nAll done. ✅\n"
    PROMPTS_MISMATCH = "## Prompt 1 -- \xa75: thing\n\n\U0001f534 still open\n"
    PROMPTS_MATCH = "## Prompt 1 -- \xa75: thing\n\n✅ done\n"

    @staticmethod
    def _fixed_documents(monkeypatch, plan, prompts):
        documents = {"plan.md": plan, "claude_code_prompts.md": prompts}
        real_read_text = Path.read_text
        real_exists = Path.exists

        def _read_text(self, *args, **kwargs):
            if self.name in documents:
                return documents[self.name]
            return real_read_text(self, *args, **kwargs)

        def _exists(self, *args, **kwargs):
            return True if self.name in documents else real_exists(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _read_text)
        monkeypatch.setattr(Path, "exists", _exists)

    def test_strict_mode_exits_one_when_the_documents_disagree(self, monkeypatch, capsys):
        self._fixed_documents(monkeypatch, self.PLAN, self.PROMPTS_MISMATCH)

        code = _run_script_as_main(PROMPT_DOC_SYNC_PATH, ["--strict"], monkeypatch)

        assert code == 1
        out = capsys.readouterr().out
        assert "Prompt 1 claims 'open' but plan.md \xa75 reads 'done'" in out

    def test_strict_mode_exits_zero_when_the_documents_agree(self, monkeypatch, capsys):
        self._fixed_documents(monkeypatch, self.PLAN, self.PROMPTS_MATCH)

        code = _run_script_as_main(PROMPT_DOC_SYNC_PATH, ["--strict"], monkeypatch)

        assert code == 0
        assert "no status mismatches found" in capsys.readouterr().out

    def test_default_mode_is_advisory_and_exits_zero_even_on_a_mismatch(
        self, monkeypatch, capsys
    ):
        self._fixed_documents(monkeypatch, self.PLAN, self.PROMPTS_MISMATCH)

        code = _run_script_as_main(PROMPT_DOC_SYNC_PATH, [], monkeypatch)

        assert code == 0
        assert "1 candidate mismatch" in capsys.readouterr().out
