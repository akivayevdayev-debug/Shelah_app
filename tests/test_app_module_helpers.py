"""
Direct tests for app.py module-level helpers that no route reaches on its own:
the /ask response cache, RAG text helpers, the lazily-imported Supabase client
factories, request user-id resolution, the IP-geolocation fallback behind
get_engine(), and the secondary-context gatherer used by /ask.

Every module global these touch (caches, SDK handles, Supabase settings) is
restored by monkeypatch, so nothing leaks between tests.
"""

from __future__ import annotations

import sys
import types
from concurrent.futures import Future

import pytest
import requests
import responses as responses_lib

import app as flask_app_module


# ─── /ask response cache ────────────────────────────────────────────────────

@pytest.fixture
def ask_cache(monkeypatch):
    cache: dict = {}
    monkeypatch.setattr(flask_app_module, "ASK_RESPONSE_CACHE", cache)
    return cache


class TestGetCachedAskPayload:
    def test_unknown_key_is_a_miss(self, ask_cache):
        assert flask_app_module._get_cached_ask_payload("missing") is None

    def test_fresh_entry_is_returned_as_a_detached_copy(self, ask_cache, monkeypatch):
        monkeypatch.setattr(flask_app_module.time, "time", lambda: 1000.0)
        ask_cache["k"] = {"ts": 1000.0, "payload": {"answer": "yes", "sources": [1, 2]}}

        first = flask_app_module._get_cached_ask_payload("k")
        first["answer"] = "mutated"
        first["sources"].append(3)

        assert flask_app_module._get_cached_ask_payload("k") == {"answer": "yes", "sources": [1, 2]}

    def test_expired_entry_is_a_miss_and_is_evicted(self, ask_cache, monkeypatch):
        ttl = flask_app_module.ASK_RESPONSE_CACHE_TTL_SECONDS
        monkeypatch.setattr(flask_app_module.time, "time", lambda: 1000.0 + ttl + 1)
        ask_cache["k"] = {"ts": 1000.0, "payload": {"answer": "old"}}

        assert flask_app_module._get_cached_ask_payload("k") is None
        assert "k" not in ask_cache

    def test_entry_exactly_at_the_ttl_is_still_served(self, ask_cache, monkeypatch):
        ttl = flask_app_module.ASK_RESPONSE_CACHE_TTL_SECONDS
        monkeypatch.setattr(flask_app_module.time, "time", lambda: 1000.0 + ttl)
        ask_cache["k"] = {"ts": 1000.0, "payload": {"answer": "edge"}}

        assert flask_app_module._get_cached_ask_payload("k") == {"answer": "edge"}

    @pytest.mark.parametrize("entry", [{}, {"ts": 0}, {"payload": None}])
    def test_malformed_entries_are_misses(self, ask_cache, monkeypatch, entry):
        monkeypatch.setattr(flask_app_module.time, "time", lambda: 0.0)
        ask_cache["k"] = entry

        assert flask_app_module._get_cached_ask_payload("k") is None

    def test_non_dict_payload_is_a_miss(self, ask_cache, monkeypatch):
        monkeypatch.setattr(flask_app_module.time, "time", lambda: 1.0)
        ask_cache["k"] = {"ts": 1.0, "payload": ["not", "a", "dict"]}

        assert flask_app_module._get_cached_ask_payload("k") is None

    def test_payload_that_cannot_round_trip_through_json_is_a_miss(self, ask_cache, monkeypatch):
        monkeypatch.setattr(flask_app_module.time, "time", lambda: 1.0)
        ask_cache["k"] = {"ts": 1.0, "payload": {"answer": {1, 2, 3}}}  # sets are not JSON

        assert flask_app_module._get_cached_ask_payload("k") is None


class TestSetCachedAskPayload:
    def test_stores_a_detached_copy_with_a_timestamp(self, ask_cache, monkeypatch):
        monkeypatch.setattr(flask_app_module.time, "time", lambda: 42.0)
        payload = {"answer": "yes", "sources": [1]}

        flask_app_module._set_cached_ask_payload("k", payload)
        payload["sources"].append(2)

        assert ask_cache["k"] == {"ts": 42.0, "payload": {"answer": "yes", "sources": [1]}}

    @pytest.mark.parametrize("payload", [None, "text", ["list"], 5])
    def test_non_dict_payloads_are_not_cached(self, ask_cache, payload):
        flask_app_module._set_cached_ask_payload("k", payload)

        assert ask_cache == {}

    def test_an_unserialisable_payload_is_dropped_without_raising(self, ask_cache):
        flask_app_module._set_cached_ask_payload("k", {"answer": {1, 2}})

        assert ask_cache == {}


# ─── RAG text helpers ───────────────────────────────────────────────────────

class TestNormalizeRagText:
    def test_collapses_whitespace(self):
        assert flask_app_module._normalize_rag_text("  a \n\t b   c ") == "a b c"

    def test_truncates_with_an_ellipsis_and_no_trailing_space(self):
        assert flask_app_module._normalize_rag_text("abc def ghi", max_chars=8) == "abc def..."

    def test_text_at_the_limit_is_untouched(self):
        assert flask_app_module._normalize_rag_text("abcdefgh", max_chars=8) == "abcdefgh"

    @pytest.mark.parametrize("max_chars", [0, -1])
    def test_non_positive_limit_disables_truncation(self, max_chars):
        text = "x" * 500

        assert flask_app_module._normalize_rag_text(text, max_chars=max_chars) == text

    def test_none_becomes_empty(self):
        assert flask_app_module._normalize_rag_text(None) == ""


class TestBuildInteractionSummary:
    def test_strips_markdown_from_the_answer_and_labels_both_halves(self):
        summary = flask_app_module._build_interaction_summary(
            "May I light candles?", "**Yes** - see `Shabbat 2a` > _really_",
        )

        assert summary == "Q: May I light candles? | A: Yes see Shabbat 2a really"

    def test_bounds_question_and_answer_lengths(self):
        summary = flask_app_module._build_interaction_summary("q" * 400, "a" * 400)

        question, answer = summary.split(" | ")
        assert question == "Q: " + "q" * 160 + "..."
        assert answer == "A: " + "a" * 240 + "..."

    def test_empty_inputs_give_the_bare_labels(self):
        assert flask_app_module._build_interaction_summary(None, None) == "Q:  | A:"


# ─── Supabase client factories ──────────────────────────────────────────────

@pytest.fixture
def supabase_settings(monkeypatch):
    """A loaded SDK stub plus URL/keys, with every created client recorded."""
    created: list[dict] = []

    def fake_create_client(url, key, options=None):
        client = types.SimpleNamespace(url=url, key=key, options=options)
        created.append({"client": client})
        return client

    class FakeOptions:
        def __init__(self, headers):
            self.headers = headers

    monkeypatch.setattr(flask_app_module, "_supabase_loaded", True)
    monkeypatch.setattr(flask_app_module, "create_client", fake_create_client)
    monkeypatch.setattr(flask_app_module, "SyncClientOptions", FakeOptions)
    monkeypatch.setattr(flask_app_module, "_supabase_client", None)
    monkeypatch.setattr(flask_app_module, "SUPABASE_URL", "https://project.invalid")
    monkeypatch.setattr(flask_app_module, "SUPABASE_SECRET_KEY", "secret-key")
    monkeypatch.setattr(flask_app_module, "SUPABASE_PUBLISHABLE_KEY", "publishable-key")
    monkeypatch.setattr(flask_app_module, "STRICT_SUPABASE_RLS", True)
    return created


class TestEnsureSupabaseLoaded:
    @pytest.fixture(autouse=True)
    def _unloaded(self, monkeypatch):
        monkeypatch.setattr(flask_app_module, "_supabase_loaded", False)
        monkeypatch.setattr(flask_app_module, "create_client", None)
        monkeypatch.setattr(flask_app_module, "SyncClientOptions", None)

    @staticmethod
    def _install_sdk(monkeypatch, *, client_options=True):
        sdk = types.ModuleType("supabase")
        sdk.create_client = lambda *args, **kwargs: "client"
        lib = types.ModuleType("supabase.lib")
        options_module = types.ModuleType("supabase.lib.client_options")
        options_module.SyncClientOptions = type("SyncClientOptions", (), {})
        monkeypatch.setitem(sys.modules, "supabase", sdk)
        monkeypatch.setitem(sys.modules, "supabase.lib", lib)
        monkeypatch.setitem(
            sys.modules, "supabase.lib.client_options", options_module if client_options else None,
        )
        return sdk, options_module

    def test_loads_the_client_factory_and_options_class_once(self, monkeypatch):
        sdk, options_module = self._install_sdk(monkeypatch)

        flask_app_module._ensure_supabase_loaded()

        assert flask_app_module.create_client is sdk.create_client
        assert flask_app_module.SyncClientOptions is options_module.SyncClientOptions
        assert flask_app_module._supabase_loaded is True

    def test_a_second_call_is_a_no_op(self, monkeypatch):
        self._install_sdk(monkeypatch)
        flask_app_module._ensure_supabase_loaded()
        sentinel = object()
        monkeypatch.setattr(flask_app_module, "create_client", sentinel)

        flask_app_module._ensure_supabase_loaded()

        assert flask_app_module.create_client is sentinel

    def test_older_sdk_without_client_options_still_loads_the_factory(self, monkeypatch):
        sdk, _ = self._install_sdk(monkeypatch, client_options=False)

        flask_app_module._ensure_supabase_loaded()

        assert flask_app_module.create_client is sdk.create_client
        assert flask_app_module.SyncClientOptions is None
        assert flask_app_module._supabase_loaded is True

    def test_missing_sdk_leaves_both_handles_unset(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "supabase", None)

        flask_app_module._ensure_supabase_loaded()

        assert flask_app_module.create_client is None
        assert flask_app_module.SyncClientOptions is None
        assert flask_app_module._supabase_loaded is True


class TestGetSupabaseClient:
    def test_builds_one_secret_key_client_and_reuses_it(self, supabase_settings):
        first = flask_app_module._get_supabase_client()
        second = flask_app_module._get_supabase_client()

        assert first is second
        assert (first.url, first.key) == ("https://project.invalid", "secret-key")
        assert len(supabase_settings) == 1

    def test_no_sdk_means_no_client(self, supabase_settings, monkeypatch):
        monkeypatch.setattr(flask_app_module, "create_client", None)

        assert flask_app_module._get_supabase_client() is None

    @pytest.mark.parametrize("setting", ["SUPABASE_URL", "SUPABASE_SECRET_KEY"])
    def test_missing_settings_mean_no_client(self, supabase_settings, monkeypatch, setting):
        monkeypatch.setattr(flask_app_module, setting, "")

        assert flask_app_module._get_supabase_client() is None
        assert supabase_settings == []


class TestGetRequestSupabaseClient:
    def test_no_sdk_means_no_client(self, supabase_settings, monkeypatch):
        monkeypatch.setattr(flask_app_module, "create_client", None)

        assert flask_app_module._get_request_supabase_client("Bearer tok") is None

    @pytest.mark.parametrize("setting", ["SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY"])
    def test_missing_settings_mean_no_client(self, supabase_settings, monkeypatch, setting):
        monkeypatch.setattr(flask_app_module, setting, "")

        assert flask_app_module._get_request_supabase_client("Bearer tok") is None

    def test_without_a_token_the_client_is_anonymous(self, supabase_settings):
        client = flask_app_module._get_request_supabase_client("")

        assert (client.url, client.key, client.options) == (
            "https://project.invalid", "publishable-key", None,
        )

    def test_a_token_is_forwarded_as_a_bearer_header(self, supabase_settings):
        client = flask_app_module._get_request_supabase_client("Bearer user-jwt")

        assert client.key == "publishable-key"
        assert client.options.headers == {"Authorization": "Bearer user-jwt"}

    def test_a_token_without_options_support_falls_back_to_the_anonymous_client(
        self, supabase_settings, monkeypatch
    ):
        monkeypatch.setattr(flask_app_module, "SyncClientOptions", None)

        client = flask_app_module._get_request_supabase_client("Bearer user-jwt")

        assert client.options is None

    def test_an_sdk_that_rejects_options_falls_back_to_the_anonymous_client(
        self, supabase_settings, monkeypatch
    ):
        calls = []

        def picky_create_client(url, key, **kwargs):
            calls.append(kwargs)
            if kwargs:
                raise TypeError("unexpected keyword argument 'options'")
            return types.SimpleNamespace(url=url, key=key, options=None)

        monkeypatch.setattr(flask_app_module, "create_client", picky_create_client)

        client = flask_app_module._get_request_supabase_client("Bearer user-jwt")

        assert client.options is None
        assert [bool(kwargs) for kwargs in calls] == [True, False]


class TestGetUserScopedSupabaseClient:
    def test_no_client_means_none(self, supabase_settings, monkeypatch):
        monkeypatch.setattr(flask_app_module, "create_client", None)

        assert flask_app_module._get_user_scoped_supabase_client("Bearer tok") is None

    def test_strict_rls_refuses_an_anonymous_client(self, supabase_settings):
        assert flask_app_module._get_user_scoped_supabase_client("") is None

    def test_strict_rls_allows_a_token_bearing_client(self, supabase_settings):
        client = flask_app_module._get_user_scoped_supabase_client("Bearer user-jwt")

        assert client.options.headers == {"Authorization": "Bearer user-jwt"}

    def test_relaxed_rls_allows_an_anonymous_client(self, supabase_settings, monkeypatch):
        monkeypatch.setattr(flask_app_module, "STRICT_SUPABASE_RLS", False)

        assert flask_app_module._get_user_scoped_supabase_client("") is not None


# ─── _get_request_user_id ───────────────────────────────────────────────────

class TestGetRequestUserId:
    def test_uses_the_verified_claims_already_on_g(self):
        with flask_app_module.app.test_request_context():
            flask_app_module.g.clerk_claims = {"sub": "  user_123  "}

            assert flask_app_module._get_request_user_id() == "user_123"

    def test_verifies_the_bearer_token_when_claims_are_absent(self, monkeypatch):
        verified = []
        monkeypatch.setattr(
            flask_app_module, "_verify_clerk_token",
            lambda token: verified.append(token) or {"sub": " user_456 "},
        )

        with flask_app_module.app.test_request_context(headers={"Authorization": "Bearer abc.def.ghi"}):
            assert flask_app_module._get_request_user_id() == "user_456"

        assert verified == ["abc.def.ghi"]

    def test_no_claims_and_no_token_means_anonymous(self):
        with flask_app_module.app.test_request_context():
            assert flask_app_module._get_request_user_id() is None

    def test_a_token_that_fails_verification_means_anonymous(self, monkeypatch):
        def reject(token):
            raise ValueError("bad signature")

        monkeypatch.setattr(flask_app_module, "_verify_clerk_token", reject)

        with flask_app_module.app.test_request_context(headers={"Authorization": "Bearer bad"}):
            assert flask_app_module._get_request_user_id() is None

    @pytest.mark.parametrize("decoded", [{}, {"sub": None}, {"sub": "   "}])
    def test_verified_claims_without_a_usable_subject_mean_anonymous(self, monkeypatch, decoded):
        monkeypatch.setattr(flask_app_module, "_verify_clerk_token", lambda token: decoded)

        with flask_app_module.app.test_request_context(headers={"Authorization": "Bearer ok"}):
            assert flask_app_module._get_request_user_id() is None


# ─── IP-geolocation fallback behind get_engine() ────────────────────────────

class TestLookupLatLonFromIp:
    IP_API = "http://ip-api.com/json/"
    IPWHO = "https://ipwho.is/"

    @responses_lib.activate
    def test_falls_through_to_the_second_provider(self):
        responses_lib.add(responses_lib.GET, self.IP_API, json={"status": "fail"}, status=200)
        responses_lib.add(
            responses_lib.GET, self.IPWHO, json={"success": True, "latitude": 51.5, "longitude": -0.12},
        )

        with flask_app_module.app.test_request_context():
            assert flask_app_module._lookup_lat_lon_from_ip() == (51.5, -0.12)

        assert len(responses_lib.calls) == 2

    @responses_lib.activate
    def test_first_provider_success_stops_the_search(self):
        responses_lib.add(
            responses_lib.GET, self.IP_API, json={"status": "success", "lat": 31.77, "lon": 35.21},
        )

        with flask_app_module.app.test_request_context():
            assert flask_app_module._lookup_lat_lon_from_ip() == (31.77, 35.21)

        assert len(responses_lib.calls) == 1

    @responses_lib.activate
    def test_a_non_ok_response_is_treated_as_no_data(self):
        responses_lib.add(responses_lib.GET, self.IP_API, json={"status": "success", "lat": 1, "lon": 2}, status=503)
        responses_lib.add(responses_lib.GET, self.IPWHO, json={}, status=500)

        with flask_app_module.app.test_request_context():
            assert flask_app_module._lookup_lat_lon_from_ip() == (None, None)

        assert len(responses_lib.calls) == 2

    @responses_lib.activate
    def test_a_network_error_gives_no_location_instead_of_raising(self):
        responses_lib.add(responses_lib.GET, self.IP_API, body=requests.ConnectionError("offline"))

        with flask_app_module.app.test_request_context():
            assert flask_app_module._lookup_lat_lon_from_ip() == (None, None)

    @responses_lib.activate
    def test_a_real_client_ip_is_used_as_the_lookup_target(self):
        responses_lib.add(responses_lib.GET, f"{self.IP_API}203.0.113.9", json={"status": "success", "lat": 1, "lon": 2})

        with flask_app_module.app.test_request_context(environ_base={"REMOTE_ADDR": "203.0.113.9"}):
            assert flask_app_module._lookup_lat_lon_from_ip() == (1.0, 2.0)

        assert "/203.0.113.9" in responses_lib.calls[0].request.url


    @responses_lib.activate
    @pytest.mark.parametrize("loopback", ["127.0.0.1", "::1"])
    def test_a_loopback_client_ip_is_never_sent_to_the_providers(self, loopback):
        responses_lib.add(responses_lib.GET, self.IP_API, json={"status": "fail"})
        responses_lib.add(responses_lib.GET, self.IPWHO, json={"success": False})

        with flask_app_module.app.test_request_context(environ_base={"REMOTE_ADDR": loopback}):
            assert flask_app_module._lookup_lat_lon_from_ip() == (None, None)

        urls = [call.request.url for call in responses_lib.calls]
        assert urls == [
            "http://ip-api.com/json/?fields=status,lat,lon,timezone,query",
            "https://ipwho.is/",
        ]


class TestGetEngineLocationFallback:
    @pytest.fixture(autouse=True)
    def _record_engine(self, monkeypatch):
        self.engines = []
        monkeypatch.setattr(
            flask_app_module, "ShelahEngine",
            lambda lat, lon: self.engines.append((lat, lon)) or (lat, lon),
        )

    def test_a_session_location_is_used_without_any_lookup(self, monkeypatch):
        def no_lookup():
            raise AssertionError("no IP lookup expected")

        monkeypatch.setattr(flask_app_module, "_lookup_lat_lon_from_ip", no_lookup)

        with flask_app_module.app.test_request_context():
            flask_app_module.session["lat"] = 31.77
            flask_app_module.session["lon"] = 35.21

            assert flask_app_module.get_engine() == (31.77, 35.21)

    def test_an_ip_lookup_result_is_remembered_in_the_session(self, monkeypatch):
        monkeypatch.setattr(flask_app_module, "_lookup_lat_lon_from_ip", lambda: (51.5, -0.12))

        with flask_app_module.app.test_request_context():
            assert flask_app_module.get_engine() == (51.5, -0.12)

            assert flask_app_module.session["lat"] == 51.5
            assert flask_app_module.session["lon"] == -0.12
            assert flask_app_module.session.permanent is True

    def test_when_every_lookup_fails_it_defaults_to_new_york_without_storing_it(self, monkeypatch):
        monkeypatch.setattr(flask_app_module, "_lookup_lat_lon_from_ip", lambda: (None, None))

        with flask_app_module.app.test_request_context():
            assert flask_app_module.get_engine() == (40.7128, -74.0060)

            assert "lat" not in flask_app_module.session


# ─── Secondary /ask context gathering ───────────────────────────────────────

def _done(value=None, error=None):
    future: Future = Future()
    if error is not None:
        future.set_exception(error)
    else:
        future.set_result(value)
    return future


class TestGatherAskQuestionContextFutures:
    @pytest.fixture
    def submissions(self, monkeypatch):
        """Replace the thread pool submit with a recorder returning prepared futures."""
        recorded = []
        results = {}

        def fake_submit(pool, fn, *args, **kwargs):
            recorded.append((getattr(fn, "__name__", repr(fn)), args, kwargs))
            return results[getattr(fn, "__name__", repr(fn))]

        monkeypatch.setattr(flask_app_module, "submit_with_context", fake_submit)
        return recorded, results

    @staticmethod
    def _engine():
        def get_halachipedia_summary(question):
            raise AssertionError("never called directly")

        def get_wiki(question):
            raise AssertionError("never called directly")

        return types.SimpleNamespace(get_halachipedia_summary=get_halachipedia_summary, get_wiki=get_wiki)

    def test_collects_each_lookups_result(self, submissions, monkeypatch):
        recorded, results = submissions
        results.update({
            "get_halachipedia_summary": _done({"title": "Shabbat"}),
            "_retrieve_community_knowledge": _done([{"id": 1}]),
            "_fetch_user_memory_summaries": _done(["prefers concise answers"]),
            "get_wiki": _done("wiki text"),
        })
        monkeypatch.setattr(flask_app_module, "_retrieve_community_knowledge", _named("_retrieve_community_knowledge"))
        monkeypatch.setattr(flask_app_module, "_fetch_user_memory_summaries", _named("_fetch_user_memory_summaries"))

        outcome = flask_app_module._gather_ask_question_context_futures(
            "candles?", "conservative", "user_1", self._engine())

        assert outcome == ({"title": "Shabbat"}, [{"id": 1}], ["prefers concise answers"], "wiki text")
        by_name = {name: (args, kwargs) for name, args, kwargs in recorded}
        assert by_name["_retrieve_community_knowledge"] == (
            ("candles?",),
            {"canonical_lens": "conservative", "max_rows": flask_app_module.RAG_TOP_KNOWLEDGE_ROWS},
        )
        assert by_name["_fetch_user_memory_summaries"] == (
            ("user_1",), {"limit": flask_app_module.RAG_MEMORY_ROWS},
        )

    def test_each_failed_lookup_degrades_to_its_own_empty_default(self, submissions, monkeypatch):
        _, results = submissions
        boom = RuntimeError("upstream down")
        results.update({
            "get_halachipedia_summary": _done(error=boom),
            "_retrieve_community_knowledge": _done(error=boom),
            "_fetch_user_memory_summaries": _done(error=boom),
            "get_wiki": _done(error=boom),
        })
        monkeypatch.setattr(flask_app_module, "_retrieve_community_knowledge", _named("_retrieve_community_knowledge"))
        monkeypatch.setattr(flask_app_module, "_fetch_user_memory_summaries", _named("_fetch_user_memory_summaries"))

        outcome = flask_app_module._gather_ask_question_context_futures(
            "candles?", "conservative", "user_1", self._engine())

        assert outcome == (None, [], [], None)

    def test_lookups_fail_independently(self, submissions, monkeypatch):
        _, results = submissions
        results.update({
            "get_halachipedia_summary": _done(error=TimeoutError("slow")),
            "_retrieve_community_knowledge": _done([{"id": 7}]),
            "_fetch_user_memory_summaries": _done(error=RuntimeError("db")),
            "get_wiki": _done("wiki text"),
        })
        monkeypatch.setattr(flask_app_module, "_retrieve_community_knowledge", _named("_retrieve_community_knowledge"))
        monkeypatch.setattr(flask_app_module, "_fetch_user_memory_summaries", _named("_fetch_user_memory_summaries"))

        outcome = flask_app_module._gather_ask_question_context_futures(
            "candles?", "conservative", "user_1", self._engine())

        assert outcome == (None, [{"id": 7}], [], "wiki text")


def _named(name):
    """A stand-in callable whose __name__ keys the fake submit's result table."""
    def stand_in(*args, **kwargs):
        raise AssertionError("submitted to the pool, never called inline")

    stand_in.__name__ = name
    return stand_in
