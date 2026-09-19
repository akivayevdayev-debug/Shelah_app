"""
Behavioural coverage tests for a handful of previously-unexercised branches in
four independent backend modules:

- backend/logging_setup.py   (exc_text passthrough, JSON fallback encoder,
                              Discord context truncation, mitigation
                              breadcrumbs, module-level Sentry init)
- backend/utils/text_engine.py (attribution-note stripping fallbacks, "# "
                              header demotion, non-UI "Key: value" lines,
                              blank-line trimming, empty-answer short circuit,
                              citation title handling)
- backend/zmanim_engine.py   (Hebcal day payload parsing and the small
                              _compute_* helpers)
- backend/cost_meter.py      (unpriced-model breadcrumb failure, settle with no
                              client, usage-fetch failure, malformed RPC shape)

Every test asserts on return values, raised errors, calls made or logged
events -- not just on execution.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import backend.cost_meter as cost_meter
import backend.logging_setup as logging_setup
import backend.utils.text_engine as text_engine
import backend.zmanim_engine as ze
from backend.utils.text_engine import (
    _collapse_markdown_spacing,
    _format_key_value_line,
    _format_ui_answer,
    _normalize_answer_line,
    _strip_source_attribution_prefix,
    format_source_citation,
)


# ═══════════════════════════════════════════════════════════════════════════
# backend/logging_setup.py
# ═══════════════════════════════════════════════════════════════════════════

def _log_record(msg="hello", **attrs):
    record = logging.LogRecord(
        name="cov.logger", level=logging.INFO, pathname=__file__,
        lineno=7, msg=msg, args=(), exc_info=None,
    )
    for key, value in attrs.items():
        setattr(record, key, value)
    return record


class _FailsOnFirstStringify:
    """An extra-field value whose str() raises the first time and succeeds
    afterwards -- the only way json.dumps(default=str) can fail on the first
    attempt yet succeed on the fallback attempt."""

    def __init__(self):
        self.calls = 0

    def __str__(self):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("first stringification fails")
        return "recovered"


class TestJSONFormatterExcTextAndFallback:
    def test_cached_exc_text_is_emitted_when_there_is_no_exc_info(self):
        record = _log_record("failed earlier", exc_text="Traceback (cached text)")
        output = json.loads(logging_setup._JSONFormatter().format(record))
        assert output["exception"] == "Traceback (cached text)"

    def test_live_exc_info_takes_precedence_over_cached_exc_text(self):
        import sys
        try:
            raise ValueError("live-boom")
        except ValueError:
            record = _log_record("failed", exc_text="stale cached text")
            record.exc_info = sys.exc_info()
        output = json.loads(logging_setup._JSONFormatter().format(record))
        assert "live-boom" in output["exception"]
        assert "stale cached text" not in output["exception"]

    def test_no_exception_key_without_exc_info_or_exc_text(self):
        output = json.loads(logging_setup._JSONFormatter().format(_log_record()))
        assert "exception" not in output

    def test_failed_first_encode_falls_back_to_ascii_json_with_message(self):
        flaky = _FailsOnFirstStringify()
        record = _log_record("שלום", flaky=flaky)

        raw = logging_setup._JSONFormatter().format(record)

        # The fallback path serialises with ensure_ascii=True: the primary path
        # (ensure_ascii=False) would have left the Hebrew text as-is.
        assert raw.isascii()
        assert "\\u05e9" in raw
        decoded = json.loads(raw)
        assert decoded["message"] == "שלום"
        assert decoded["flaky"] == "recovered"
        # first attempt raised, the fallback attempt stringified it again.
        assert flaky.calls == 2

    def test_healthy_record_keeps_non_ascii_unescaped(self):
        raw = logging_setup._JSONFormatter().format(_log_record("שלום"))
        assert "שלום" in raw


class TestDiscordWebhookContextTruncation:
    @staticmethod
    def _context_field(body):
        fields = body["embeds"][0]["fields"]
        return next((f for f in fields if f["name"] == "context"), None)

    @staticmethod
    def _payload_with_json_length(total_len):
        # json.dumps({"k": "<n x's>"}) is 9 + n characters long.
        return {"event": "e", "message": "m", "context": {"k": "x" * (total_len - 9)}}

    def test_oversized_context_is_cut_to_950_chars_plus_ellipsis(self):
        payload = self._payload_with_json_length(2000)
        full = json.dumps(payload["context"], ensure_ascii=True)

        field = self._context_field(logging_setup._discord_webhook_body(payload))

        assert field["value"] == f"```{full[:950]}…```"
        assert field["inline"] is False

    def test_context_of_exactly_950_chars_is_not_truncated(self):
        payload = self._payload_with_json_length(950)
        full = json.dumps(payload["context"], ensure_ascii=True)
        assert len(full) == 950

        field = self._context_field(logging_setup._discord_webhook_body(payload))

        assert field["value"] == f"```{full}```"
        assert "…" not in field["value"]

    def test_context_of_951_chars_is_truncated(self):
        payload = self._payload_with_json_length(951)
        full = json.dumps(payload["context"], ensure_ascii=True)

        field = self._context_field(logging_setup._discord_webhook_body(payload))

        assert field["value"] == f"```{full[:950]}…```"

    def test_empty_context_adds_no_context_field(self):
        body = logging_setup._discord_webhook_body(
            {"event": "e", "message": "m", "context": {}, "request_id": "rid-1"})
        assert self._context_field(body) is None
        assert body["embeds"][0]["fields"] == [
            {"name": "request_id", "value": "rid-1", "inline": True}]


class _FakeSentry:
    def __init__(self, raises=None):
        self.breadcrumbs = []
        self._raises = raises

    def add_breadcrumb(self, **kwargs):
        if self._raises is not None:
            raise self._raises
        self.breadcrumbs.append(kwargs)


class TestLogMitigationBreadcrumb:
    def test_breadcrumb_recorded_when_sentry_enabled(self, monkeypatch):
        fake = _FakeSentry()
        monkeypatch.setattr(logging_setup, "_sentry_enabled", True)
        monkeypatch.setattr(logging_setup, "sentry_sdk", fake)

        logging_setup.log_mitigation("waf", "ask", "hash-abc", "/ask")

        assert fake.breadcrumbs == [{
            "category": "mitigation",
            "message": "waf:ask",
            "level": "info",
            "data": {"key_hash": "hash-abc", "route": "/ask"},
        }]

    def test_breadcrumb_message_joins_tier_and_route_class(self, monkeypatch):
        fake = _FakeSentry()
        monkeypatch.setattr(logging_setup, "_sentry_enabled", True)
        monkeypatch.setattr(logging_setup, "sentry_sdk", fake)

        logging_setup.log_mitigation("breaker", "fanout", "h", "/api/geocode")

        assert fake.breadcrumbs[0]["message"] == "breaker:fanout"
        assert fake.breadcrumbs[0]["data"]["route"] == "/api/geocode"

    def test_no_breadcrumb_when_sentry_disabled(self, monkeypatch):
        fake = _FakeSentry()
        monkeypatch.setattr(logging_setup, "_sentry_enabled", False)
        monkeypatch.setattr(logging_setup, "sentry_sdk", fake)

        logging_setup.log_mitigation("waf", "ask", "hash-abc", "/ask")

        assert fake.breadcrumbs == []

    def test_breadcrumb_failure_is_swallowed_and_log_line_still_emitted(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(logging_setup, "_sentry_enabled", True)
        monkeypatch.setattr(
            logging_setup, "sentry_sdk", _FakeSentry(raises=RuntimeError("sentry down")))

        with caplog.at_level(logging.INFO, logger="shelah.mitigation"):
            logging_setup.log_mitigation("middleware", "ask", "hash-xyz", "/ask")

        records = [r for r in caplog.records if r.name == "shelah.mitigation"]
        assert len(records) == 1
        assert records[0].getMessage() == "mitigation_triggered"
        assert records[0].tier == "middleware"
        assert records[0].key_hash == "hash-xyz"


class TestModuleLevelSentryInit:
    """Lines 165-172 of logging_setup run once at import. Re-executing the
    module file under a throw-away name exercises them without reloading (and
    so without resetting the state of) the real, already-imported module."""

    @staticmethod
    def _import_fresh_copy():
        path = Path(logging_setup.__file__)
        spec = importlib.util.spec_from_file_location("_logging_setup_import_probe", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_dsn_in_env_initialises_sentry_with_built_kwargs(self, monkeypatch):
        import sentry_sdk
        init_calls = []
        monkeypatch.setattr(sentry_sdk, "init", lambda **kw: init_calls.append(kw))
        monkeypatch.setenv("SENTRY_DSN", "  https://key@o1.ingest.us.sentry.io/1  ")

        module = self._import_fresh_copy()

        assert module._sentry_enabled is True
        assert len(init_calls) == 1
        # Whitespace around the DSN is stripped before use.
        assert init_calls[0]["dsn"] == "https://key@o1.ingest.us.sentry.io/1"
        assert init_calls[0] == module._build_sentry_init_kwargs(
            "https://key@o1.ingest.us.sentry.io/1")
        assert init_calls[0]["send_default_pii"] is False

    @pytest.mark.parametrize("dsn", [None, "", "   "])
    def test_missing_or_blank_dsn_never_initialises_sentry(self, monkeypatch, dsn):
        import sentry_sdk
        init_calls = []
        monkeypatch.setattr(sentry_sdk, "init", lambda **kw: init_calls.append(kw))
        if dsn is None:
            monkeypatch.delenv("SENTRY_DSN", raising=False)
        else:
            monkeypatch.setenv("SENTRY_DSN", dsn)

        module = self._import_fresh_copy()

        assert module._sentry_enabled is False
        assert init_calls == []

    def test_init_failure_leaves_sentry_disabled_and_import_succeeds(self, monkeypatch):
        import sentry_sdk

        def _boom(**kwargs):
            raise RuntimeError("bad dsn")

        monkeypatch.setattr(sentry_sdk, "init", _boom)
        monkeypatch.setenv("SENTRY_DSN", "https://key@o1.ingest.us.sentry.io/1")

        module = self._import_fresh_copy()

        assert module._sentry_enabled is False

    def test_real_module_state_is_untouched_by_the_probe(self, monkeypatch):
        import sentry_sdk
        monkeypatch.setattr(sentry_sdk, "init", lambda **kw: None)
        monkeypatch.setenv("SENTRY_DSN", "https://key@o1.ingest.us.sentry.io/1")
        before = logging_setup._sentry_enabled

        self._import_fresh_copy()

        assert logging_setup._sentry_enabled is before


# ═══════════════════════════════════════════════════════════════════════════
# backend/utils/text_engine.py
# ═══════════════════════════════════════════════════════════════════════════

NOTE_PREFIX = "Note: ⚠️ This is educational information only."


class TestStripSourceAttributionPrefixFallbacks:
    def test_note_line_without_blank_line_or_footer_drops_only_first_line(self):
        text = f"{NOTE_PREFIX}\nThe actual answer.\nSecond body line."
        assert _strip_source_attribution_prefix(text) == "The actual answer.\nSecond body line."

    def test_remaining_lines_are_left_stripped(self):
        text = f"{NOTE_PREFIX}\n   \n  Indented answer"
        assert _strip_source_attribution_prefix(text) == "Indented answer"

    def test_single_line_note_with_nothing_else_is_returned_unchanged(self):
        assert _strip_source_attribution_prefix(NOTE_PREFIX) == NOTE_PREFIX

    def test_non_note_text_passes_through_untouched(self):
        assert _strip_source_attribution_prefix("Plain\nanswer") == "Plain\nanswer"

    def test_blank_line_shape_still_wins_over_first_line_fallback(self):
        text = f"{NOTE_PREFIX}\n\nBody\nmore"
        assert _strip_source_attribution_prefix(text) == "Body\nmore"


class TestNormalizeAnswerLineHeadersAndKeys:
    def test_single_hash_title_is_demoted_to_level_two(self):
        assert _normalize_answer_line("# My Title  ") == ["## My Title"]

    def test_single_hash_demotion_collapses_extra_space_after_hash(self):
        assert _normalize_answer_line("#   Spaced Title") == ["## Spaced Title"]

    def test_hash_without_space_is_not_treated_as_header(self):
        assert _normalize_answer_line("#hashtag") == ["#hashtag"]

    def test_double_hash_header_is_left_alone(self):
        assert _normalize_answer_line("## Already Level Two") == ["## Already Level Two"]

    def test_key_value_with_non_ui_key_is_not_promoted_to_a_header(self):
        assert _normalize_answer_line("Note: this is permitted") == [
            "Note: this is **Permitted**"]

    def test_format_key_value_line_returns_none_for_non_ui_key(self):
        assert _format_key_value_line("Author: Rambam") is None

    def test_format_key_value_line_promotes_a_ui_key(self):
        assert _format_key_value_line("Reason: it is forbidden") == [
            "### Reason", "it is **Forbidden**"]

    def test_format_key_value_line_none_when_line_is_not_key_value_shaped(self):
        assert _format_key_value_line("no colon here") is None


class TestCollapseMarkdownSpacingTrimming:
    def test_trailing_blank_line_is_removed(self):
        assert _collapse_markdown_spacing(["a", ""]) == ["a"]

    def test_multiple_trailing_blank_lines_are_removed(self):
        assert _collapse_markdown_spacing(["a", "", "", ""]) == ["a"]

    def test_internal_single_blank_line_is_preserved(self):
        assert _collapse_markdown_spacing(["a", "", "b", ""]) == ["a", "", "b"]

    def test_leading_blank_entries_are_trimmed_defensively(self, monkeypatch):
        """With the real step function a leading blank can never be appended
        (prev_blank starts True), so the leading trim is defence-in-depth.
        Substitute a step that appends everything to prove the trimming loops
        drop blank entries at both ends and keep the body."""
        def _append_everything(text, normalized, prev_blank):
            normalized.append(text)
            return False

        monkeypatch.setattr(text_engine, "_collapse_markdown_spacing_step", _append_everything)

        assert _collapse_markdown_spacing(["", "   ", "body", "", "  "]) == ["body"]

    def test_trim_stops_at_first_non_blank_entry(self, monkeypatch):
        def _append_everything(text, normalized, prev_blank):
            normalized.append(text)
            return False

        monkeypatch.setattr(text_engine, "_collapse_markdown_spacing_step", _append_everything)

        assert _collapse_markdown_spacing(["", "a", "", "b", ""]) == ["a", "", "b"]


class TestFormatUiAnswerEmptyAndTrailing:
    @pytest.mark.parametrize("value", ["", None, "   \n \n"])
    def test_blank_input_yields_empty_string(self, value):
        assert _format_ui_answer(value) == ""

    def test_answer_made_only_of_debug_lines_yields_empty_string(self):
        text = "Conflict flags: none\n- conflict flag: minor\nSource: community knowledge"
        assert _format_ui_answer(text) == ""

    def test_trailing_blank_lines_do_not_leak_into_output(self):
        assert _format_ui_answer("plain text\n\n\n") == "## Ruling\n\nplain text"

    def test_hash_title_answer_is_rendered_with_demoted_header(self):
        assert _format_ui_answer("# Shabbat\nbody text") == "## Shabbat\nbody text"


class TestFormatSourceCitationTitleHandling:
    def test_empty_ref_returns_stripped_title(self):
        assert format_source_citation("", "  Genesis  ") == "Genesis"

    def test_none_ref_returns_title(self):
        assert format_source_citation(None, "Berakhot") == "Berakhot"

    def test_whitespace_ref_returns_title(self):
        assert format_source_citation("   ", "Berakhot") == "Berakhot"

    def test_empty_ref_and_no_title_returns_empty_string(self):
        assert format_source_citation("") == ""
        assert format_source_citation(None, None) == ""

    def test_title_missing_from_citation_is_prepended_with_parenthesised_citation(self):
        assert format_source_citation("Genesis.1.1", title="Bereshit") == "Bereshit (Genesis 1:1)"

    def test_underscored_ref_is_normalised_inside_the_parentheses(self):
        assert format_source_citation(
            "Orach_Chayim.242.1", title="Shulchan Arukh") == "Shulchan Arukh (Orach Chayim 242:1)"

    def test_title_already_present_case_insensitively_is_not_repeated(self):
        assert format_source_citation("Genesis.1.1", title="genesis") == "Genesis 1:1"

    def test_no_title_returns_bare_citation(self):
        assert format_source_citation("Genesis.1.1") == "Genesis 1:1"


# ═══════════════════════════════════════════════════════════════════════════
# backend/zmanim_engine.py
# ═══════════════════════════════════════════════════════════════════════════

NYC_LAT, NYC_LON, NYC_TZ = 40.7128, -74.006, "America/New_York"


@pytest.fixture
def _clean_hebcal_caches():
    ze._HEBCAL_DAY_CACHE.clear()
    ze._HEBCAL_MONTH_CACHE.clear()
    yield
    ze._HEBCAL_DAY_CACHE.clear()
    ze._HEBCAL_MONTH_CACHE.clear()


class _FakeHebcalResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class TestHebcalDayPayloadParsing:
    def test_same_day_candles_and_havdalah_are_parsed_and_other_days_ignored(
        self, monkeypatch, _clean_hebcal_caches
    ):
        payload = {"items": [
            {"date": "2026-01-02T16:31:00-05:00", "category": "candles"},
            {"date": "2026-01-02T17:45:00-05:00", "category": "havdalah"},
            {"date": "2026-01-02", "category": "parashat", "title": "Parashat Vayechi"},
            # Other days that must never overwrite the requested day's values.
            {"date": "2026-01-03T17:46:00-05:00", "category": "havdalah"},
            {"date": "2026-01-09T16:38:00-05:00", "category": "candles"},
        ]}
        requests_made = []

        def _fake_get(url, timeout=None):
            requests_made.append((url, timeout))
            return _FakeHebcalResponse(payload)

        monkeypatch.setattr(ze._HTTP, "get", _fake_get)

        result = ze._get_hebcal_day_times(NYC_LAT, NYC_LON, NYC_TZ, date(2026, 1, 2))

        assert result["candles"] == datetime.fromisoformat("2026-01-02T16:31:00-05:00")
        assert result["havdalah"] == datetime.fromisoformat("2026-01-02T17:45:00-05:00")
        assert len(requests_made) == 1
        url, timeout = requests_made[0]
        assert timeout == 6
        assert "latitude=40.7128" in url
        assert "longitude=-74.006" in url
        assert f"tzid={NYC_TZ}" in url
        assert "year=2026&month=1" in url

    def test_day_with_no_matching_items_leaves_both_times_none(
        self, monkeypatch, _clean_hebcal_caches
    ):
        payload = {"items": [{"date": "2026-01-03T17:46:00-05:00", "category": "havdalah"}]}
        monkeypatch.setattr(
            ze._HTTP, "get", lambda url, timeout=None: _FakeHebcalResponse(payload))

        result = ze._get_hebcal_day_times(NYC_LAT, NYC_LON, NYC_TZ, date(2026, 1, 2))

        assert result == {"candles": None, "havdalah": None}

    def test_parsed_result_is_cached_for_the_same_key(self, monkeypatch, _clean_hebcal_caches):
        payload = {"items": [{"date": "2026-01-02T16:31:00-05:00", "category": "candles"}]}
        calls = []

        def _fake_get(url, timeout=None):
            calls.append(url)
            return _FakeHebcalResponse(payload)

        monkeypatch.setattr(ze._HTTP, "get", _fake_get)

        first = ze._get_hebcal_day_times(NYC_LAT, NYC_LON, NYC_TZ, date(2026, 1, 2))
        second = ze._get_hebcal_day_times(NYC_LAT, NYC_LON, NYC_TZ, date(2026, 1, 2))

        assert first == second
        assert first["candles"] == datetime.fromisoformat("2026-01-02T16:31:00-05:00")
        assert len(calls) == 1


class _StubCalendar:
    """Stands in for zmanim's ZmanimCalendar for the *_fallback helpers."""

    def __init__(self, candle_lighting_value):
        self._value = candle_lighting_value
        self.candle_lighting_calls = 0

    def candle_lighting(self):
        self.candle_lighting_calls += 1
        return self._value


class TestComputeCandleLighting:
    HEBCAL = datetime(2026, 1, 2, 16, 31)
    LIBRARY = datetime(2026, 1, 2, 16, 20)

    def test_hebcal_value_wins_and_library_is_not_consulted(self):
        calendar = _StubCalendar(self.LIBRARY)

        result = ze._compute_candle_lighting(self.HEBCAL, True, False, calendar)

        assert result == self.HEBCAL
        assert calendar.candle_lighting_calls == 0

    def test_hebcal_value_used_for_yom_tov_eve_too(self):
        calendar = _StubCalendar(self.LIBRARY)
        assert ze._compute_candle_lighting(self.HEBCAL, False, True, calendar) == self.HEBCAL

    def test_library_value_used_when_hebcal_missing(self):
        calendar = _StubCalendar(self.LIBRARY)

        result = ze._compute_candle_lighting(None, True, False, calendar)

        assert result == self.LIBRARY
        assert calendar.candle_lighting_calls == 1

    def test_nothing_shown_when_neither_friday_nor_yom_tov_eve(self):
        calendar = _StubCalendar(self.LIBRARY)

        assert ze._compute_candle_lighting(self.HEBCAL, False, False, calendar) is None
        assert calendar.candle_lighting_calls == 0


class TestComputeHavdalah:
    HEBCAL = datetime(2026, 1, 3, 17, 45)
    NIGHTFALL = datetime(2026, 1, 3, 17, 30)

    def test_hebcal_value_preferred_on_shabbat(self):
        assert ze._compute_havdalah(self.HEBCAL, True, False, self.NIGHTFALL) == self.HEBCAL

    def test_hebcal_value_preferred_on_last_day_of_holiday(self):
        assert ze._compute_havdalah(self.HEBCAL, False, True, self.NIGHTFALL) == self.HEBCAL

    def test_falls_back_to_three_star_nightfall_without_hebcal(self):
        assert ze._compute_havdalah(None, True, False, self.NIGHTFALL) == self.NIGHTFALL

    def test_nothing_shown_on_an_ordinary_day_even_with_hebcal_value(self):
        assert ze._compute_havdalah(self.HEBCAL, False, False, self.NIGHTFALL) is None


class TestComputeMidnight:
    SUNSET = datetime(2026, 1, 2, 16, 40)
    CHATZOS = datetime(2026, 1, 2, 12, 0)

    def test_midpoint_between_sunset_and_next_dawn_when_available(self):
        next_alos = self.SUNSET + timedelta(hours=14)
        assert ze._compute_midnight(self.SUNSET, next_alos, self.CHATZOS) == (
            self.SUNSET + timedelta(hours=7))

    def test_falls_back_to_chatzos_plus_twelve_hours_without_sunset(self):
        assert ze._compute_midnight(None, self.SUNSET + timedelta(hours=14), self.CHATZOS) == (
            self.CHATZOS + timedelta(hours=12))

    def test_falls_back_to_chatzos_plus_twelve_hours_without_next_dawn(self):
        assert ze._compute_midnight(self.SUNSET, None, self.CHATZOS) == (
            self.CHATZOS + timedelta(hours=12))

    def test_dawn_not_after_sunset_is_rejected_in_favour_of_fallback(self):
        # next dawn equal to (not after) sunset is nonsensical for a midpoint.
        assert ze._compute_midnight(self.SUNSET, self.SUNSET, self.CHATZOS) == (
            self.CHATZOS + timedelta(hours=12))
        assert ze._compute_midnight(self.SUNSET, self.SUNSET - timedelta(hours=1), self.CHATZOS) == (
            self.CHATZOS + timedelta(hours=12))

    def test_none_when_no_usable_inputs(self):
        assert ze._compute_midnight(None, None, None) is None

    def test_none_when_dawn_unusable_and_no_chatzos(self):
        assert ze._compute_midnight(self.SUNSET, None, None) is None


class TestComputeShabbatWarning:
    NOW = datetime(2026, 1, 2, 16, 0)
    WARNING = "Shabbat is approaching! Less than 18 minutes to sunset."

    def _sunset_in(self, minutes):
        return self.NOW + timedelta(minutes=minutes)

    def test_warning_when_friday_and_sunset_within_window(self):
        assert ze._compute_shabbat_warning(True, self._sunset_in(10), self.NOW) == self.WARNING

    def test_warning_at_exactly_eighteen_minutes(self):
        assert ze._compute_shabbat_warning(True, self._sunset_in(18), self.NOW) == self.WARNING

    def test_warning_just_before_sunset(self):
        assert ze._compute_shabbat_warning(True, self._sunset_in(1), self.NOW) == self.WARNING

    def test_no_warning_beyond_eighteen_minutes(self):
        assert ze._compute_shabbat_warning(True, self._sunset_in(19), self.NOW) == ""

    def test_no_warning_at_the_exact_moment_of_sunset(self):
        assert ze._compute_shabbat_warning(True, self.NOW, self.NOW) == ""

    def test_no_warning_after_sunset(self):
        assert ze._compute_shabbat_warning(True, self._sunset_in(-5), self.NOW) == ""

    def test_no_warning_when_not_friday(self):
        assert ze._compute_shabbat_warning(False, self._sunset_in(10), self.NOW) == ""

    def test_no_warning_without_a_sunset_time(self):
        assert ze._compute_shabbat_warning(True, None, self.NOW) == ""

    def test_timezone_aware_datetimes_are_supported(self):
        tz = timezone(timedelta(hours=-5))
        now = datetime(2026, 1, 2, 16, 0, tzinfo=tz)
        sunset = datetime(2026, 1, 2, 16, 12, 30, tzinfo=tz)
        assert ze._compute_shabbat_warning(True, sunset, now) == self.WARNING


# ═══════════════════════════════════════════════════════════════════════════
# backend/cost_meter.py
# ═══════════════════════════════════════════════════════════════════════════

class _CostMeterFakeSentry:
    def __init__(self, raises=None):
        self.breadcrumbs = []
        self._raises = raises

    def add_breadcrumb(self, **kwargs):
        if self._raises is not None:
            raise self._raises
        self.breadcrumbs.append(kwargs)


class TestUnpricedModelBreadcrumb:
    @pytest.fixture(autouse=True)
    def _isolated_warned_set(self, monkeypatch):
        monkeypatch.setattr(cost_meter, "_WARNED_UNPRICED_MODELS", set())

    def test_breadcrumb_is_recorded_for_an_unpriced_model(self, monkeypatch):
        fake = _CostMeterFakeSentry()
        monkeypatch.setattr(cost_meter, "sentry_sdk", fake)

        assert cost_meter.estimate_cost_usd("Cov-Unpriced-Model", 10, 10) == 0.0

        assert fake.breadcrumbs == [{
            "category": "cost_meter",
            "message": "Unpriced model: Cov-Unpriced-Model",
            "level": "warning",
        }]

    def test_breadcrumb_failure_is_swallowed_and_warning_still_logged(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            cost_meter, "sentry_sdk", _CostMeterFakeSentry(raises=RuntimeError("sentry down")))

        with caplog.at_level(logging.WARNING, logger="backend.cost_meter"):
            result = cost_meter.estimate_cost_usd("cov-unpriced-model", 10, 10)

        assert result == 0.0
        assert any(
            "cov-unpriced-model" in record.getMessage() and record.levelno == logging.WARNING
            for record in caplog.records
        )
        # Still marked as warned, so a broken Sentry cannot cause log spam.
        assert "cov-unpriced-model" in cost_meter._WARNED_UNPRICED_MODELS

    def test_second_call_records_no_second_breadcrumb(self, monkeypatch):
        fake = _CostMeterFakeSentry()
        monkeypatch.setattr(cost_meter, "sentry_sdk", fake)

        cost_meter.estimate_cost_usd("cov-unpriced-model", 1, 1)
        cost_meter.estimate_cost_usd("COV-UNPRICED-MODEL", 1, 1)

        assert len(fake.breadcrumbs) == 1

    def test_missing_sentry_sdk_is_tolerated(self, monkeypatch, caplog):
        monkeypatch.setattr(cost_meter, "sentry_sdk", None)

        with caplog.at_level(logging.WARNING, logger="backend.cost_meter"):
            assert cost_meter.estimate_cost_usd("cov-unpriced-model", 1, 1) == 0.0

        assert any("cov-unpriced-model" in r.getMessage() for r in caplog.records)


class TestSettleUsageReservationWithoutClient:
    def test_no_client_means_nothing_is_written_or_reported(self, monkeypatch):
        import app as flask_app_module
        monkeypatch.setattr(flask_app_module, "_get_supabase_client", lambda: None)
        inserted, errors = [], []
        monkeypatch.setattr(cost_meter, "_insert_usage_row", lambda row: inserted.append(row))
        monkeypatch.setattr(
            cost_meter, "_capture_backend_error", lambda *a, **k: errors.append((a, k)))

        result = cost_meter._settle_usage_reservation(
            "resv-none", {"provider": "gemini", "model": "m", "route": "/ask"})

        assert result is None
        assert inserted == []
        assert errors == []


class _RaisingClient:
    def table(self, name):
        raise RuntimeError("supabase unreachable")


class _PagedThenFailingQuery:
    """Returns one full page on the first execute(), then raises."""

    def __init__(self, first_page):
        self._first_page = first_page
        self.executed = 0

    def select(self, *a, **k):
        return self

    def gte(self, *a, **k):
        return self

    def range(self, *a, **k):
        return self

    def execute(self):
        self.executed += 1
        if self.executed == 1:
            return type("R", (), {"data": self._first_page})()
        raise RuntimeError("connection reset mid-pagination")


class _PagedThenFailingClient:
    def __init__(self, first_page):
        self.query = _PagedThenFailingQuery(first_page)

    def table(self, name):
        return self.query


class TestFetchTodayUsageRowsFailure:
    def test_client_error_returns_empty_list_and_logs_at_debug(self, monkeypatch, caplog):
        import app as flask_app_module
        monkeypatch.setattr(flask_app_module, "_get_supabase_client", lambda: _RaisingClient())

        with caplog.at_level(logging.DEBUG, logger="backend.cost_meter"):
            rows = cost_meter._fetch_today_usage_rows()

        assert rows == []
        skipped = [r for r in caplog.records if "usage fetch skipped" in r.getMessage()]
        assert len(skipped) == 1
        assert skipped[0].levelno == logging.DEBUG
        assert "supabase unreachable" in skipped[0].getMessage()

    def test_client_factory_error_returns_empty_list(self, monkeypatch):
        import app as flask_app_module

        def _boom():
            raise RuntimeError("no credentials")

        monkeypatch.setattr(flask_app_module, "_get_supabase_client", _boom)

        assert cost_meter._fetch_today_usage_rows() == []

    def test_error_after_a_successful_page_discards_partial_rows(self, monkeypatch):
        import app as flask_app_module
        full_page = [{"cost_usd": 0.01}] * cost_meter._USAGE_FETCH_PAGE_SIZE
        client = _PagedThenFailingClient(full_page)
        monkeypatch.setattr(flask_app_module, "_get_supabase_client", lambda: client)

        assert cost_meter._fetch_today_usage_rows() == []
        assert client.query.executed == 2


class _RpcResult:
    def __init__(self, data):
        self.data = data


class _RpcClient:
    def __init__(self, data):
        self._data = data
        self.rpc_calls = []

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        return self

    def execute(self):
        return _RpcResult(self._data)


_FAIL_OPEN = {"allowed": True, "total_usd": 0.0, "reservation_id": ""}


class TestReserveBudgetMalformedRpcResponse:
    @pytest.mark.parametrize(
        "data",
        [
            None,
            [],
            {"allowed": False, "total_usd": 9.0},          # bare dict, not a row list
            ["not-a-dict"],
            [None],
            [{"total_usd": 5.0, "reservation_id": "r"}],   # row without "allowed"
        ],
        ids=["none", "empty-list", "bare-dict", "non-dict-row", "none-row", "missing-allowed"],
    )
    def test_unrecognised_shapes_fail_open(self, monkeypatch, data):
        import app as flask_app_module
        client = _RpcClient(data)
        monkeypatch.setattr(flask_app_module, "_get_supabase_client", lambda: client)

        result = cost_meter._reserve_budget_or_deny("user_id", "user-1", 2.0, 0.02)

        assert result == _FAIL_OPEN
        assert client.rpc_calls == [(
            "check_and_reserve_user_budget",
            {
                "p_key_column": "user_id",
                "p_key_value": "user-1",
                "p_threshold_usd": 2.0,
                "p_reservation_usd": 0.02,
            },
        )]

    def test_well_formed_denial_is_still_honoured(self, monkeypatch):
        import app as flask_app_module
        client = _RpcClient([{"allowed": False, "total_usd": 2.5, "reservation_id": None}])
        monkeypatch.setattr(flask_app_module, "_get_supabase_client", lambda: client)

        result = cost_meter._reserve_budget_or_deny("user_id", "user-1", 2.0, 0.02)

        assert result == {"allowed": False, "total_usd": 2.5, "reservation_id": ""}
