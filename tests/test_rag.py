"""
Coverage-expansion tests for backend/rag.py.

rag.py resolves its `app` collaborator lazily (`import app as _app` inside
each function body) specifically to avoid a circular import with app.py's
own re-import shims (see module docstring). Tests therefore monkeypatch
attributes directly on the real `app` module rather than passing a fake
module in, since that's the actual integration seam these functions use.
"""

from __future__ import annotations

import pytest

import backend.rag as rag
import app


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """Chainable fake query builder covering the subset rag.py uses,
    including ilike/or_ which tests/test_routes_user.py's fake doesn't need."""

    def __init__(self, data=None, error=None):
        self._data = data if data is not None else []
        self._error = error
        self.calls = []

    def table(self, name):
        self.calls.append(("table", name))
        return self

    def select(self, *a, **k):
        self.calls.append(("select", a, k))
        return self

    def insert(self, *a, **k):
        self.calls.append(("insert", a, k))
        return self

    def ilike(self, *a, **k):
        self.calls.append(("ilike", a, k))
        return self

    def or_(self, *a, **k):
        self.calls.append(("or_", a, k))
        return self

    def eq(self, *a, **k):
        self.calls.append(("eq", a, k))
        return self

    def order(self, *a, **k):
        self.calls.append(("order", a, k))
        return self

    def limit(self, *a, **k):
        self.calls.append(("limit", a, k))
        return self

    def execute(self):
        if self._error is not None:
            raise self._error
        return _FakeResult(self._data)


class _FakeSupabaseClient:
    def __init__(self, data=None, error=None):
        self.query = _FakeQuery(data=data, error=error)

    def table(self, name):
        self.query.table(name)
        return self.query


# ─────────────────────────── _compose_answer_with_prefixes ─────────────────

class TestComposeAnswerWithPrefixes:
    def test_empty_body_returns_empty(self):
        assert rag._compose_answer_with_prefixes("") == ""
        assert rag._compose_answer_with_prefixes(None) == ""

    def test_body_only_no_prefixes(self):
        assert rag._compose_answer_with_prefixes("The ruling.") == "The ruling."

    def test_with_web_warning(self, monkeypatch):
        monkeypatch.setattr(app, "WEB_LAST_RESORT_WARNING", "WARN")
        result = rag._compose_answer_with_prefixes("Body.", include_web_warning=True)
        assert result == "WARN\n\nBody."

    def test_with_attribution_only(self):
        result = rag._compose_answer_with_prefixes("Body.", source_attribution_note="Note: x.")
        assert result == "Note: x.\n\nBody."

    def test_with_both_web_warning_and_attribution(self, monkeypatch):
        monkeypatch.setattr(app, "WEB_LAST_RESORT_WARNING", "WARN")
        result = rag._compose_answer_with_prefixes(
            "Body.", include_web_warning=True, source_attribution_note="Note: x.",
        )
        assert result == "WARN\n\nNote: x.\n\nBody."


# ─────────────────────────── _build_ask_tool_context ───────────────────────

class TestBuildAskToolContext:
    def test_includes_base_context_fields(self, monkeypatch):
        monkeypatch.setattr(app, "CLERK_ENFORCE_AUTH", True)
        engine = type("Engine", (), {"get_zmanim": lambda self, x: {}})()
        context = rag._build_ask_tool_context(engine)
        assert context["route"] == "/ask"
        assert context["auth_enforced"] is True

    def test_includes_zmanim_snapshot_when_available(self, monkeypatch):
        monkeypatch.setattr(app, "CLERK_ENFORCE_AUTH", False)
        engine = type("Engine", (), {
            "get_zmanim": lambda self, x: {
                "metadata": {"date": "2026-01-01", "hebrew_date": "1 Shevat", "parasha": "Vaera", "holiday": None, "timezone": "UTC"},
                "zmanim": {"Sunrise": "6:45 AM", "Sunset": "5:15 PM", "N/A field": "N/A"},
            },
        })()
        context = rag._build_ask_tool_context(engine)
        assert context["civil_date"] == "2026-01-01"
        assert context["zmanim_snapshot"]["Sunrise"] == "6:45 AM"
        assert "N/A field" not in context["zmanim_snapshot"]

    def test_resilient_to_engine_exception(self, monkeypatch):
        monkeypatch.setattr(app, "CLERK_ENFORCE_AUTH", False)
        engine = type("Engine", (), {"get_zmanim": lambda self, x: (_ for _ in ()).throw(RuntimeError("boom"))})()
        context = rag._build_ask_tool_context(engine)
        assert context["route"] == "/ask"
        assert "civil_date" not in context


# ─────────────────────────── scoring / filtering helpers ───────────────────

class TestScoreCommunityKnowledgeRow:
    def test_keyword_match_in_topic_scores_highest(self):
        row = {"topic": "shabbat candles", "halakhic_source": "", "content": "", "community_name": ""}
        score = rag._score_community_knowledge_row(row, ["shabbat"], "All")
        assert score == 8

    def test_lens_match_adds_bonus(self):
        row = {"topic": "", "halakhic_source": "", "content": "", "community_name": "Ashkenaz"}
        score = rag._score_community_knowledge_row(row, [], "Ashkenaz")
        assert score == 8

    def test_no_match_scores_zero(self):
        row = {"topic": "x", "halakhic_source": "y", "content": "z", "community_name": "w"}
        score = rag._score_community_knowledge_row(row, ["shabbat"], "All")
        assert score == 0

    def test_multiple_keyword_hits_across_fields_sum(self):
        row = {"topic": "shabbat", "halakhic_source": "shabbat laws", "content": "shabbat candles", "community_name": ""}
        score = rag._score_community_knowledge_row(row, ["shabbat"], "All")
        assert score == 8 + 4 + 2


class TestCommunityFilterFromRequest:
    def test_explicit_lens_used_directly(self):
        assert rag._community_filter_from_request("query", "Ashkenaz") == "Ashkenaz"

    def test_falls_back_to_detection_when_lens_is_all(self, monkeypatch):
        monkeypatch.setattr(app, "_detect_community_in_text", lambda q: "Sefardic")
        assert rag._community_filter_from_request("some sefardic query", "All") == "Sefardic"

    def test_no_detection_returns_none(self, monkeypatch):
        monkeypatch.setattr(app, "_detect_community_in_text", lambda q: None)
        assert rag._community_filter_from_request("generic query", "All") is None


class TestBuildKnowledgeTextOrFilter:
    def test_builds_ilike_conditions(self):
        result = rag._build_knowledge_text_or_filter(["shabbat", "kashrut"])
        assert "topic.ilike.%shabbat%" in result
        assert "content.ilike.%kashrut%" in result

    def test_empty_keywords_returns_empty_string(self):
        assert rag._build_knowledge_text_or_filter([]) == ""
        assert rag._build_knowledge_text_or_filter(None) == ""

    def test_strips_unsafe_characters(self):
        result = rag._build_knowledge_text_or_filter(["sha'bbat!"])
        assert "shabbat" in result

    def test_caps_at_max_keywords(self):
        result = rag._build_knowledge_text_or_filter(["a", "b", "c", "d"], max_keywords=2)
        assert result.count("topic.ilike") == 2


# ─────────────────────────── _retrieve_community_knowledge ─────────────────

class TestRetrieveCommunityKnowledge:
    def test_no_supabase_client_returns_empty(self, monkeypatch):
        monkeypatch.setattr(app, "_get_supabase_client", lambda: None)
        assert rag._retrieve_community_knowledge("query") == []

    def test_happy_path_ranks_and_filters_rows(self, monkeypatch):
        rows = [
            {"id": "1", "community_name": "Ashkenaz", "topic": "shabbat candles", "halakhic_source": "SA", "content": "lighting rules"},
            {"id": "2", "community_name": "Sefardic", "topic": "unrelated topic", "halakhic_source": "X", "content": "nothing relevant"},
        ]
        monkeypatch.setattr(app, "_get_supabase_client", lambda: _FakeSupabaseClient(data=rows))
        monkeypatch.setattr(app, "RAG_TOP_KNOWLEDGE_ROWS", 5)
        monkeypatch.setattr(app, "_extract_query_keywords", lambda q, max_keywords=10: ["shabbat"])
        monkeypatch.setattr(app, "SUPABASE_COMMUNITY_KNOWLEDGE_TABLE", "community_knowledge")
        monkeypatch.setattr(app, "_normalize_rag_text", lambda text: str(text or ""))
        monkeypatch.setattr(app, "_detect_community_in_text", lambda q: None)

        result = rag._retrieve_community_knowledge("what about shabbat candles", canonical_lens="All")
        assert len(result) == 1
        assert result[0]["id"] == "1"

    def test_query_exception_returns_empty(self, monkeypatch):
        monkeypatch.setattr(app, "_get_supabase_client", lambda: _FakeSupabaseClient(error=RuntimeError("db down")))
        monkeypatch.setattr(app, "RAG_TOP_KNOWLEDGE_ROWS", 5)
        monkeypatch.setattr(app, "_extract_query_keywords", lambda q, max_keywords=10: [])
        monkeypatch.setattr(app, "SUPABASE_COMMUNITY_KNOWLEDGE_TABLE", "community_knowledge")
        monkeypatch.setattr(app, "_detect_community_in_text", lambda q: None)
        assert rag._retrieve_community_knowledge("query") == []

    def test_community_only_match_without_keyword_relevance_excluded(self, monkeypatch):
        # A row matching the community filter but with zero keyword relevance
        # should be excluded (score == community bonus only).
        rows = [{"id": "1", "community_name": "Ashkenaz", "topic": "unrelated", "halakhic_source": "", "content": "nothing to do with query"}]
        monkeypatch.setattr(app, "_get_supabase_client", lambda: _FakeSupabaseClient(data=rows))
        monkeypatch.setattr(app, "RAG_TOP_KNOWLEDGE_ROWS", 5)
        monkeypatch.setattr(app, "_extract_query_keywords", lambda q, max_keywords=10: ["shabbat"])
        monkeypatch.setattr(app, "SUPABASE_COMMUNITY_KNOWLEDGE_TABLE", "community_knowledge")
        monkeypatch.setattr(app, "_normalize_rag_text", lambda text: str(text or ""))
        monkeypatch.setattr(app, "_detect_community_in_text", lambda q: None)

        result = rag._retrieve_community_knowledge("shabbat query", canonical_lens="Ashkenaz")
        assert result == []


# ─────────────────────────── _env_int ───────────────────────────────────────

class TestEnvInt:
    def test_uses_default_when_env_var_absent(self, monkeypatch):
        monkeypatch.delenv("SOME_RAG_INT_VAR", raising=False)
        assert rag._env_int("SOME_RAG_INT_VAR", 7) == 7

    def test_int_default_branch_parses_env_var(self, monkeypatch):
        # Regression test for a previously-confirmed bug (see findings log):
        # an inverted conditional used to return int(default) instead of
        # int(raw_value) whenever the caller's default was an int — which was
        # true for both real call sites (RAG_TOP_KNOWLEDGE_ROWS, RAG_MEMORY_ROWS),
        # silently discarding any env var override. Fixed to match app.py's
        # correct implementation.
        monkeypatch.setenv("SOME_RAG_INT_VAR", "42")
        assert rag._env_int("SOME_RAG_INT_VAR", 7) == 42

    def test_string_default_branch_also_parses_env_var(self, monkeypatch):
        monkeypatch.setenv("SOME_RAG_INT_VAR", "42")
        assert rag._env_int("SOME_RAG_INT_VAR", "not-an-int-default") == 42

    def test_invalid_env_var_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("SOME_RAG_INT_VAR", "not-a-number")
        assert rag._env_int("SOME_RAG_INT_VAR", "also-not-a-number") == "also-not-a-number"


# ─────────────────────────── _knowledge_rows_to_customs ────────────────────

class TestKnowledgeRowsToCustoms:
    def test_maps_fields(self):
        rows = [{"community_name": "Ashkenaz", "topic": "Shabbat", "content": "Light candles.", "halakhic_source": "SA"}]
        result = rag._knowledge_rows_to_customs(rows)
        assert result[0]["community"] == "Ashkenaz"
        assert result[0]["ruling"] == "Light candles."
        assert result[0]["notes"] == "Topic: Shabbat"

    def test_skips_non_dict_rows(self):
        assert rag._knowledge_rows_to_customs(["not a dict"]) == []

    def test_empty_topic_yields_empty_notes(self):
        rows = [{"community_name": "X", "topic": "", "content": "c", "halakhic_source": "s"}]
        result = rag._knowledge_rows_to_customs(rows)
        assert result[0]["notes"] == ""


# ─────────────────────────── _fetch_user_memory_summaries ──────────────────

class TestFetchUserMemorySummaries:
    def test_no_user_id_returns_empty(self):
        assert rag._fetch_user_memory_summaries(None) == []
        assert rag._fetch_user_memory_summaries("") == []

    def test_no_client_returns_empty(self, monkeypatch):
        monkeypatch.setattr(app, "_get_user_scoped_supabase_client", lambda: None)
        monkeypatch.setattr(app, "STRICT_SUPABASE_RLS", True)
        assert rag._fetch_user_memory_summaries("user-1") == []

    def test_falls_back_to_service_client_when_not_strict_rls(self, monkeypatch):
        rows = [{"summary": "Asked about kashrut.", "created_at": "2026-01-01"}]
        monkeypatch.setattr(app, "_get_user_scoped_supabase_client", lambda: None)
        monkeypatch.setattr(app, "STRICT_SUPABASE_RLS", False)
        monkeypatch.setattr(app, "_get_supabase_client", lambda: _FakeSupabaseClient(data=rows))
        monkeypatch.setattr(app, "SUPABASE_USER_MEMORIES_TABLE", "user_memories")
        monkeypatch.setattr(app, "_normalize_rag_text", lambda text, max_chars=260: str(text or ""))
        result = rag._fetch_user_memory_summaries("user-1")
        assert result[0]["summary"] == "Asked about kashrut."

    def test_query_exception_returns_empty(self, monkeypatch):
        monkeypatch.setattr(app, "_get_user_scoped_supabase_client", lambda: _FakeSupabaseClient(error=RuntimeError("boom")))
        monkeypatch.setattr(app, "STRICT_SUPABASE_RLS", True)
        monkeypatch.setattr(app, "SUPABASE_USER_MEMORIES_TABLE", "user_memories")
        assert rag._fetch_user_memory_summaries("user-1") == []

    def test_skips_rows_with_empty_summary(self, monkeypatch):
        rows = [{"summary": "", "created_at": "x"}, {"summary": "real", "created_at": "y"}]
        monkeypatch.setattr(app, "_get_user_scoped_supabase_client", lambda: _FakeSupabaseClient(data=rows))
        monkeypatch.setattr(app, "STRICT_SUPABASE_RLS", True)
        monkeypatch.setattr(app, "SUPABASE_USER_MEMORIES_TABLE", "user_memories")
        monkeypatch.setattr(app, "_normalize_rag_text", lambda text, max_chars=260: str(text or ""))
        result = rag._fetch_user_memory_summaries("user-1")
        assert len(result) == 1
        assert result[0]["summary"] == "real"


# ─────────────────────────── _store_ask_history ─────────────────────────────

class TestStoreAskHistory:
    def test_no_user_id_does_nothing(self, monkeypatch):
        client = _FakeSupabaseClient()
        monkeypatch.setattr(app, "_get_supabase_client", lambda: client)
        rag._store_ask_history(None, "q", "a")
        assert client.query.calls == []

    def test_no_client_does_nothing(self, monkeypatch):
        monkeypatch.setattr(app, "_get_supabase_client", lambda: None)
        rag._store_ask_history("user-1", "q", "a")  # should not raise

    def test_happy_path_inserts_payload(self, monkeypatch):
        client = _FakeSupabaseClient()
        monkeypatch.setattr(app, "_get_supabase_client", lambda: client)
        monkeypatch.setattr(app, "SUPABASE_ASK_HISTORY_TABLE", "ask_history")
        rag._store_ask_history("user-1", "question", "answer", sources=[{"ref": "x"}])
        insert_calls = [c for c in client.query.calls if c[0] == "insert"]
        assert len(insert_calls) == 1
        payload = insert_calls[0][1][0]
        assert payload["user_id"] == "user-1"
        assert payload["question"] == "question"

    def test_insert_exception_swallowed(self, monkeypatch):
        client = _FakeSupabaseClient(error=RuntimeError("insert failed"))
        monkeypatch.setattr(app, "_get_supabase_client", lambda: client)
        monkeypatch.setattr(app, "SUPABASE_ASK_HISTORY_TABLE", "ask_history")
        rag._store_ask_history("user-1", "q", "a")  # should not raise


# ─────────────────────────── _store_user_memory_summary ────────────────────

class TestStoreUserMemorySummary:
    def test_no_user_id_does_nothing(self, monkeypatch):
        client = _FakeSupabaseClient()
        monkeypatch.setattr(app, "_get_user_scoped_supabase_client", lambda: client)
        rag._store_user_memory_summary(None, "q", "a")
        assert client.query.calls == []

    def test_no_summary_built_does_nothing(self, monkeypatch):
        client = _FakeSupabaseClient()
        monkeypatch.setattr(app, "_get_user_scoped_supabase_client", lambda: client)
        monkeypatch.setattr(app, "STRICT_SUPABASE_RLS", True)
        monkeypatch.setattr(app, "_build_interaction_summary", lambda q, a: "")
        rag._store_user_memory_summary("user-1", "q", "a")
        assert client.query.calls == []

    def test_happy_path_inserts_summary(self, monkeypatch):
        client = _FakeSupabaseClient()
        monkeypatch.setattr(app, "_get_user_scoped_supabase_client", lambda: client)
        monkeypatch.setattr(app, "STRICT_SUPABASE_RLS", True)
        monkeypatch.setattr(app, "_build_interaction_summary", lambda q, a: "User asked about kashrut.")
        monkeypatch.setattr(app, "SUPABASE_USER_MEMORIES_TABLE", "user_memories")
        rag._store_user_memory_summary("user-1", "q", "a")
        insert_calls = [c for c in client.query.calls if c[0] == "insert"]
        assert len(insert_calls) == 1
        assert insert_calls[0][1][0]["summary"] == "User asked about kashrut."

    def test_insert_exception_swallowed(self, monkeypatch):
        client = _FakeSupabaseClient(error=RuntimeError("boom"))
        monkeypatch.setattr(app, "_get_user_scoped_supabase_client", lambda: client)
        monkeypatch.setattr(app, "STRICT_SUPABASE_RLS", True)
        monkeypatch.setattr(app, "_build_interaction_summary", lambda q, a: "summary")
        monkeypatch.setattr(app, "SUPABASE_USER_MEMORIES_TABLE", "user_memories")
        rag._store_user_memory_summary("user-1", "q", "a")  # should not raise
