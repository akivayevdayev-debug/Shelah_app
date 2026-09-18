"""Tests for scripts/verify_integrations.py.

The headline regression: ``_interpret_community_response`` used to return True
on every path (SonarCloud python:S3516 BLOCKER + pythonbugs:S2583), so a 200
response with a garbage body was reported as PASS and ``main()`` could not exit
non-zero because of it. These tests pin that a malformed community payload is
now a FAIL that propagates all the way to the process exit code.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "verify_integrations.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("verify_integrations", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    # The script calls load_dotenv() at import time; don't let it pull a
    # developer's real .env into this process's environment.
    with patch("dotenv.load_dotenv"):
        spec.loader.exec_module(module)
    return module


vi = _load_script()


def _resp(status=200, payload=None, headers=None, json_error=False):
    def _json():
        if json_error:
            raise ValueError("not json")
        return payload

    return SimpleNamespace(status_code=status, json=_json, headers=headers or {})


def _patch_get(monkeypatch, response=None, exc=None):
    def fake_get(url, *args, **kwargs):
        if exc is not None:
            raise exc
        return response

    monkeypatch.setattr(vi.requests, "get", fake_get)


# ─────────────────────── the S3516 / S2583 regression ───────────────────────

class TestInterpretCommunityResponse:
    def test_identity_payload_passes_and_names_the_community(self, capsys):
        data = {"identity": {"display_name": "Ashkenaz"}, "name": "ignored"}
        assert vi._interpret_community_response(data) is True
        assert "Community API working: Ashkenaz" in capsys.readouterr().out

    def test_customs_only_payload_passes_using_top_level_name(self, capsys):
        assert vi._interpret_community_response({"customs": {}, "name": "Sephardic"}) is True
        assert "Community API working: Sephardic" in capsys.readouterr().out

    def test_non_dict_identity_falls_back_to_name(self, capsys):
        assert vi._interpret_community_response({"identity": "oops", "customs": {}}) is True
        assert "Community API working: Unknown" in capsys.readouterr().out

    @pytest.mark.parametrize("bad", [{}, {"error": "nope"}, {"name": "x"}, [], None, "<html>"])
    def test_payload_without_expected_keys_fails(self, bad, capsys):
        assert vi._interpret_community_response(bad) is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "missing the expected structure" in out


class TestCheckCommunityEndpoints:
    @pytest.fixture(autouse=True)
    def _local_base_url(self, monkeypatch):
        monkeypatch.setattr(vi, "LOCAL_BASE_URL", "http://localhost:5001")

    def test_skipped_when_flask_not_detected(self, monkeypatch):
        monkeypatch.setattr(vi, "LOCAL_BASE_URL", None)
        assert vi.check_community_endpoints() is None

    def test_good_payload_passes(self, monkeypatch):
        _patch_get(monkeypatch, _resp(200, {"name": "ashkenaz", "customs": {}}))
        assert vi.check_community_endpoints() is True

    def test_200_with_wrong_shape_fails(self, monkeypatch):
        _patch_get(monkeypatch, _resp(200, {"unrelated": True}))
        assert vi.check_community_endpoints() is False

    def test_200_with_non_json_body_fails(self, monkeypatch):
        _patch_get(monkeypatch, _resp(200, json_error=True))
        assert vi.check_community_endpoints() is False

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_restricted_is_reachable(self, monkeypatch, status):
        _patch_get(monkeypatch, _resp(status))
        assert vi.check_community_endpoints() is True

    def test_server_error_fails(self, monkeypatch):
        _patch_get(monkeypatch, _resp(500))
        assert vi.check_community_endpoints() is False

    def test_connection_error_is_skipped(self, monkeypatch):
        _patch_get(monkeypatch, exc=requests.ConnectionError("down"))
        assert vi.check_community_endpoints() is None

    def test_unexpected_exception_fails(self, monkeypatch):
        _patch_get(monkeypatch, exc=RuntimeError("boom"))
        assert vi.check_community_endpoints() is False


class TestSummaryAndExitCode:
    def test_print_summary_fails_when_any_check_failed(self, capsys):
        assert vi.print_summary({"a": True, "b": False, "c": None}) is False
        out = capsys.readouterr().out
        assert "1 passed, 1 failed, 1 skipped" in out
        assert "Some checks failed" in out

    def test_print_summary_passes_with_only_passes_and_skips(self, capsys):
        assert vi.print_summary({"a": True, "b": None}) is True
        assert "All critical checks passed" in capsys.readouterr().out

    def _stub_other_checks(self, monkeypatch):
        for name in ("check_env_variables", "check_json_files", "check_supabase",
                     "check_sefaria", "check_hebcal", "check_flask_local", "check_vercel"):
            monkeypatch.setattr(vi, name, lambda: True)

    def test_main_exits_nonzero_when_community_payload_is_malformed(self, monkeypatch):
        """End to end: a 200 with a bad body must flip the process exit code."""
        self._stub_other_checks(monkeypatch)
        monkeypatch.setattr(vi, "LOCAL_BASE_URL", "http://localhost:5001")
        _patch_get(monkeypatch, _resp(200, {"unrelated": True}))
        assert vi.main() == 1

    def test_main_exits_zero_when_everything_passes(self, monkeypatch):
        self._stub_other_checks(monkeypatch)
        monkeypatch.setattr(vi, "LOCAL_BASE_URL", "http://localhost:5001")
        _patch_get(monkeypatch, _resp(200, {"name": "ashkenaz", "customs": {}}))
        assert vi.main() == 0


# ───────────────────────────── helpers / env ─────────────────────────────

def test_get_env_value_returns_first_non_empty_candidate(monkeypatch):
    monkeypatch.setenv("VI_FIRST", "   ")
    monkeypatch.setenv("VI_SECOND", " value ")
    assert vi.get_env_value("VI_FIRST", "VI_SECOND") == ("value", "VI_SECOND")
    assert vi.get_env_value("VI_UNSET_A", "VI_UNSET_B") == ("", None)


def test_mask_value_truncates_only_long_values():
    assert vi.mask_value("short") == "short"
    long_value = "x" * 15 + "MIDDLE" * 5 + "y" * 10
    masked = vi.mask_value(long_value)
    assert masked == "x" * 15 + "..." + "y" * 10


class TestCheckEnvVariables:
    REQUIRED = ["SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY", "SUPABASE_SECRET_KEY",
                "CLERK_PUBLISHABLE_KEY", "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY",
                "CLERK_JWT_ISSUER"]
    OPTIONAL = ["SUPABASE_PREFS_TABLE", "CLERK_AUDIENCE", "CLERK_ENFORCE_AUTH", "VERCEL_URL"]

    def _clear(self, monkeypatch):
        for name in self.REQUIRED + self.OPTIONAL:
            monkeypatch.delenv(name, raising=False)

    def test_all_required_present_passes(self, monkeypatch, capsys):
        self._clear(monkeypatch)
        monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
        monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "pk")
        monkeypatch.setenv("SUPABASE_SECRET_KEY", "sk")
        monkeypatch.setenv("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", "clerk-pk")
        monkeypatch.setenv("CLERK_JWT_ISSUER", "https://clerk.example")
        monkeypatch.setenv("VERCEL_URL", "example.vercel.app")
        assert vi.check_env_variables() is True
        out = capsys.readouterr().out
        assert "from NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY" in out
        assert "VERCEL_URL" in out

    def test_missing_required_fails(self, monkeypatch, capsys):
        self._clear(monkeypatch)
        assert vi.check_env_variables() is False
        assert "SUPABASE_URL is missing or empty" in capsys.readouterr().out


class TestCheckJsonFiles:
    def _customs(self, monkeypatch, tmp_path, files):
        customs = tmp_path / "customs"
        customs.mkdir()
        for name, text in files.items():
            (customs / name).write_text(text, encoding="utf-8")
        monkeypatch.setattr(vi, "__file__", str(tmp_path / "scripts" / "verify_integrations.py"))

    def test_valid_files_pass(self, monkeypatch, tmp_path, capsys):
        self._customs(monkeypatch, tmp_path, {
            "a.json": json.dumps({"halacha_index": [{}, {}]}),
            "b.json": json.dumps({"other": 1}),
            "c.json": json.dumps([1, 2]),
        })
        assert vi.check_json_files() is True
        out = capsys.readouterr().out
        assert "a.json: Valid (2 halacha items)" in out
        assert "b.json: Unusual structure" in out
        assert "c.json: Not a dict (is list)" in out

    def test_invalid_json_fails(self, monkeypatch, tmp_path, capsys):
        self._customs(monkeypatch, tmp_path, {"bad.json": "{not json"})
        assert vi.check_json_files() is False
        assert "bad.json: Invalid JSON" in capsys.readouterr().out

    def test_empty_directory_fails(self, monkeypatch, tmp_path, capsys):
        self._customs(monkeypatch, tmp_path, {})
        assert vi.check_json_files() is False
        assert "No JSON files found" in capsys.readouterr().out


# ───────────────────────────── external APIs ─────────────────────────────

class TestCheckSupabase:
    def _env(self, monkeypatch, url="https://x.supabase.co", key="pk"):
        monkeypatch.setenv("SUPABASE_URL", url)
        monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", key)
        monkeypatch.delenv("SUPABASE_PREFS_TABLE", raising=False)

    def test_unconfigured_fails(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_PUBLISHABLE_KEY", raising=False)
        assert vi.check_supabase() is False

    def test_http_fallback_when_supabase_py_missing(self, monkeypatch):
        self._env(monkeypatch)
        monkeypatch.setitem(sys.modules, "supabase", None)  # import -> ImportError
        _patch_get(monkeypatch, _resp(401))
        assert vi.check_supabase() is True
        _patch_get(monkeypatch, _resp(500))
        assert vi.check_supabase() is False

    def _fake_supabase(self, monkeypatch, query_error=None):
        class _Query:
            def select(self, *_):
                return self

            def limit(self, *_):
                return self

            def execute(self):
                if query_error:
                    raise query_error
                return SimpleNamespace(data=[])

        client = SimpleNamespace(table=lambda name: _Query())
        monkeypatch.setitem(sys.modules, "supabase",
                            SimpleNamespace(create_client=lambda url, key: client))

    def test_client_query_success(self, monkeypatch):
        self._env(monkeypatch)
        self._fake_supabase(monkeypatch)
        assert vi.check_supabase() is True

    def test_client_query_failure_falls_back_to_project_api(self, monkeypatch):
        self._env(monkeypatch)
        self._fake_supabase(monkeypatch, query_error=RuntimeError("no table"))
        _patch_get(monkeypatch, _resp(200))
        assert vi.check_supabase() is True
        _patch_get(monkeypatch, _resp(503))
        assert vi.check_supabase() is False

    def test_timeout_and_unexpected_error_fail(self, monkeypatch):
        self._env(monkeypatch)
        monkeypatch.setitem(sys.modules, "supabase", None)
        _patch_get(monkeypatch, exc=requests.Timeout())
        assert vi.check_supabase() is False
        _patch_get(monkeypatch, exc=RuntimeError("boom"))
        assert vi.check_supabase() is False


class TestSefariaAndHebcal:
    """A reachable API that returns the WRONG thing is a FAIL, not a warning:
    these checks exist to prove the integration works, not just that a host
    answers. (They used to warn and return True on any 200 with an odd body,
    and check_sefaria's key test repeated `'he' in data`, so it could not fail.)"""

    GOOD_SEFARIA = {"he": ["בראשית ברא"], "text": ["When God began to create"]}
    GOOD_HEBCAL = {"gy": 2026, "gm": 4, "gd": 9, "hy": 5786, "hm": "Nisan", "hd": 22}

    def test_sefaria_good_payload_passes_including_nested_segment_lists(self, monkeypatch, capsys):
        _patch_get(monkeypatch, _resp(200, self.GOOD_SEFARIA))
        assert vi.check_sefaria() is True
        _patch_get(monkeypatch, _resp(200, {"he": [["א"], []], "text": [["a"]]}))
        assert vi.check_sefaria() is True
        assert "PASS" in capsys.readouterr().out

    @pytest.mark.parametrize("payload", [
        {"other": 1},                                 # neither language
        {"he": ["בראשית"]},                            # Hebrew only
        {"text": ["In the beginning"]},               # English only
        {"he": [], "text": []},                       # keys present, nothing in them
        {"he": ["בראשית"], "text": ["", "  "]},       # English is blank
        {"he": "   ", "text": "In the beginning"},    # Hebrew is blank
        {"he": None, "text": None},
        {"error": "Couldn't find a text"},            # Sefaria's 200-with-error shape
        ["he", "text"],                               # not an object
        "he text",                                    # a bare string contains both substrings
    ])
    def test_sefaria_200_with_a_wrong_or_empty_payload_fails(self, monkeypatch, capsys, payload):
        _patch_get(monkeypatch, _resp(200, payload))
        assert vi.check_sefaria() is False
        assert "FAIL" in capsys.readouterr().out

    def test_sefaria_transport_problems_fail(self, monkeypatch):
        _patch_get(monkeypatch, _resp(500))
        assert vi.check_sefaria() is False
        _patch_get(monkeypatch, exc=requests.Timeout())
        assert vi.check_sefaria() is False
        _patch_get(monkeypatch, exc=RuntimeError("boom"))
        assert vi.check_sefaria() is False
        _patch_get(monkeypatch, _resp(200, json_error=True))
        assert vi.check_sefaria() is False

    def test_hebcal_correct_conversion_passes(self, monkeypatch, capsys):
        _patch_get(monkeypatch, _resp(200, self.GOOD_HEBCAL))
        assert vi.check_hebcal() is True
        assert "PASS" in capsys.readouterr().out

    @pytest.mark.parametrize("payload", [
        {"hy": 1},                                              # missing fields
        {"hy": 1, "hm": "Nisan", "hd": 2},                      # all present, wrong date
        {"hy": 5786, "hm": "Nisan", "hd": 21},                  # off by one day
        {"hy": 5786, "hm": "Iyyar", "hd": 22},                  # wrong month
        {"hy": 5785, "hm": "Nisan", "hd": 22},                  # wrong year
        {"hy": "5786", "hm": "Nisan", "hd": "22"},              # right values, wrong types
        {"error": "Invalid date"},
        [5786, "Nisan", 22],                                    # not an object
        "hy hm hd",                                             # a bare string contains all three
    ])
    def test_hebcal_200_with_a_wrong_conversion_fails_and_says_what_it_got(self, monkeypatch, capsys, payload):
        _patch_get(monkeypatch, _resp(200, payload))
        assert vi.check_hebcal() is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "22 Nisan 5786" in out, "the failure must state the expected conversion"

    def test_hebcal_transport_problems_fail(self, monkeypatch):
        _patch_get(monkeypatch, _resp(404))
        assert vi.check_hebcal() is False
        _patch_get(monkeypatch, exc=requests.Timeout())
        assert vi.check_hebcal() is False
        _patch_get(monkeypatch, exc=RuntimeError("boom"))
        assert vi.check_hebcal() is False
        _patch_get(monkeypatch, _resp(200, json_error=True))
        assert vi.check_hebcal() is False


# ───────────────────────── local Flask + Vercel probes ─────────────────────────

class TestFlaskProbes:
    def test_health_payload_flask_true_is_detected(self, monkeypatch):
        _patch_get(monkeypatch, _resp(200, {"flask": True}))
        assert vi._check_flask_health_at_port("http://localhost:5001") is True

    def test_200_with_unexpected_or_non_json_payload_is_rejected(self, monkeypatch):
        _patch_get(monkeypatch, _resp(200, {"flask": False}))
        assert vi._check_flask_health_at_port("http://localhost:5001") is False
        _patch_get(monkeypatch, _resp(200, json_error=True))
        assert vi._check_flask_health_at_port("http://localhost:5001") is False

    def test_auth_gated_health_endpoint_counts_as_detected(self, monkeypatch):
        _patch_get(monkeypatch, _resp(401))
        assert vi._check_flask_health_at_port("http://localhost:5001") is True

    def test_airtunes_and_other_statuses_are_rejected(self, monkeypatch):
        _patch_get(monkeypatch, _resp(404, headers={"Server": "AirTunes/1.0"}))
        assert vi._check_flask_health_at_port("http://localhost:5000") is False
        _patch_get(monkeypatch, _resp(500))
        assert vi._check_flask_health_at_port("http://localhost:5000") is False

    def test_connection_error_and_unexpected_error_are_rejected(self, monkeypatch):
        _patch_get(monkeypatch, exc=requests.ConnectionError())
        assert vi._check_flask_health_at_port("http://localhost:5000") is False
        _patch_get(monkeypatch, exc=RuntimeError("boom"))
        assert vi._check_flask_health_at_port("http://localhost:5000") is False

    def test_check_flask_local_adopts_first_matching_port(self, monkeypatch):
        monkeypatch.setenv("PORT", "5055")
        seen = []

        def fake_probe(base_url):
            seen.append(base_url)
            return base_url.endswith(":5001")

        monkeypatch.setattr(vi, "_check_flask_health_at_port", fake_probe)
        assert vi.check_flask_local() is True
        assert vi.LOCAL_BASE_URL == "http://localhost:5001"
        assert seen == ["http://localhost:5055", "http://localhost:5001"]

    def test_check_flask_local_skips_when_nothing_answers(self, monkeypatch):
        monkeypatch.delenv("PORT", raising=False)
        monkeypatch.setattr(vi, "_check_flask_health_at_port", lambda base_url: False)
        assert vi.check_flask_local() is None
        assert vi.LOCAL_BASE_URL is None


class TestVercelProbes:
    def test_dedupe_normalizes_and_drops_duplicates_and_blanks(self):
        urls = ["example.vercel.app/", "https://example.vercel.app", "  ", "http://a.test"]
        assert vi._dedupe_vercel_candidate_urls(urls) == [
            "https://example.vercel.app", "http://a.test"]

    def test_health_ok_payload(self, monkeypatch):
        _patch_get(monkeypatch, _resp(200, {"flask": True}))
        assert vi._check_vercel_health_at_url("https://x") is True

    def test_health_200_with_unexpected_payload_is_still_reachable(self, monkeypatch):
        _patch_get(monkeypatch, _resp(200, json_error=True))
        assert vi._check_vercel_health_at_url("https://x") is True

    def test_access_restricted_is_reachable(self, monkeypatch):
        _patch_get(monkeypatch, _resp(403))
        assert vi._check_vercel_health_at_url("https://x") is True

    def test_other_status_falls_back_to_root_probe(self, monkeypatch):
        calls = []

        def fake_get(url, *args, **kwargs):
            calls.append(url)
            return _resp(404) if url.endswith("/api/stack/health") else _resp(200)

        monkeypatch.setattr(vi.requests, "get", fake_get)
        assert vi._check_vercel_health_at_url("https://x") is True
        assert calls == ["https://x/api/stack/health", "https://x"]

    def test_root_5xx_and_transport_errors_skip_the_url(self, monkeypatch):
        _patch_get(monkeypatch, _resp(502))
        assert vi._check_vercel_health_at_url("https://x") is None
        _patch_get(monkeypatch, exc=requests.Timeout())
        assert vi._check_vercel_health_at_url("https://x") is None
        _patch_get(monkeypatch, exc=RuntimeError("boom"))
        assert vi._check_vercel_health_at_url("https://x") is None

    def test_check_vercel_returns_true_on_first_verified_candidate(self, monkeypatch):
        monkeypatch.setenv("VERCEL_URL", "mine.vercel.app")
        monkeypatch.setenv("CLERK_AUDIENCE", "https://aud.example")
        probed = []

        def fake_probe(url):
            probed.append(url)
            return True if url == "https://aud.example" else None

        monkeypatch.setattr(vi, "_check_vercel_health_at_url", fake_probe)
        assert vi.check_vercel() is True
        assert probed == ["https://mine.vercel.app", "https://aud.example"]

    def test_check_vercel_skips_when_no_candidate_verifies(self, monkeypatch):
        monkeypatch.delenv("VERCEL_URL", raising=False)
        monkeypatch.delenv("CLERK_AUDIENCE", raising=False)
        monkeypatch.setattr(vi, "_check_vercel_health_at_url", lambda url: None)
        assert vi.check_vercel() is None

    def test_check_vercel_skips_when_there_are_no_candidates(self, monkeypatch):
        monkeypatch.setattr(vi, "_dedupe_vercel_candidate_urls", lambda urls: [])
        assert vi.check_vercel() is None
