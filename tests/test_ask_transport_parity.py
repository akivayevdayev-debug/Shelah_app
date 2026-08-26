"""
Pins the Flask (app.py) and ASGI (asgi.py) /ask handlers to each other on
plan.md §22.3.2's six invariants -- Prompt 35 STEP 2, the actual deliverable
of the ask_pipeline.py dead-code decision (plan.md §22). Only asgi.py's
route is reachable in production (plan.md §16.1-D1: FastAPI's native POST
/ask is registered before the WSGIMiddleware Flask mount, so Starlette's
router always matches it first), but both implementations are kept by
deliberate decision and must not silently drift from each other -- that is
exactly how the ai_cited_sources bug (ENGINEERING_RULES.md's "AI request
resilience" section) shipped.

This file extends, rather than duplicates, the partial parity coverage
already in tests/test_ask.py: TestAskTransportKeySetParity (success/
fallback top-level key set), TestAiCitedSourcesSchemaParity,
TestAiModelTimeoutWiring / TestAiTotalBudgetTimeout, and
TestSafetyClassMetaPropagation.

Invariant coverage map (plan.md §22.3.2):
  a) top-level key set on every path       -> TestTopLevelKeySetEveryPath
     Adds the strict-block and security-blocked paths -- the two NOT
     already covered by test_ask.py::TestAskTransportKeySetParity's
     success/fallback pair.
  b) identical retry/timeout behavior      -> TestTimeoutBudgetParity
     Adds the missing Flask half of test_ask.py::TestAiTotalBudgetTimeout
     (which only covered FastAPI) plus a same-constant assertion pinning
     both synthesis functions to the one shared claude.AI_TOTAL_BUDGET_SECONDS
     rather than a hand-rolled per-transport number.
  c) identical prompt-selection thresholds -> TestPromptSelectionThresholdParity
     Covers is_simple's propagation into render_structured_markdown() at
     the /ask call site. Found and fixed during this pass: asgi.py's call
     site silently dropped is_simple (app.py's did not), so a "simple"
     (short) question whose model answer still carried practical_steps/
     summary content rendered in FULL format on the ASGI transport --
     the one actually reachable in production -- but SIMPLE format on
     Flask. See asgi.py::_run_ask_async_ai_synthesis's render_structured_
     markdown call for the fix.
  d) classify_safety() before every synthesis call -> TestClassifySafetyInvoked
  e) per-user budget check present on both, or an explicit test asserting
     Flask is unreachable in production -> ALREADY COVERED, not duplicated
     here per the coordination note in claude_code_prompts.md Prompt 35
     STEP 2(e): tests/test_cost_meter_budget_atomicity.py::
     test_flask_ask_route_is_unreachable_behind_the_asgi_mount (built under
     Prompt 33b STEP 5). That test pins the FastAPI-route-before-Flask-
     mount registration order that makes the budget-check asymmetry safe.
  f) identical meta key set on every response -> TestMetaKeySetParity
     "cached" (Flask) / "async" (ASGI) is a deliberate, accepted per-
     transport tag -- the same exception test_ask.py::
     TestAskTransportKeySetParity's docstring already documents for the
     top-level comparison. Excluded here the same way, then every other
     meta key must match exactly.

A second, previously-undetected drift was also found and fixed during this
pass, adjacent to invariant (a): asgi.py had no dedicated branch for a
"security_blocked" result at all. It fell through into the success-shaped
formatting code, which rendered a safety-referral/domain-block message
through render_structured_markdown() as if it were a real halachic answer,
reported meta.structured=True for what is actually a non-answer, and fed
the blocked text into _store_user_memory_summary() as if it were a genuine
past answer -- wrong for a route this safety-critical, and only reachable
in production. Fixed with asgi.py::_security_blocked_ask_async_payload(),
mirroring app.py::_security_blocked_ask_payload() (which itself was
missing the top-level ai_cited_sources key -- also fixed). The tests below
exercise the fixed behavior, not the historical bug.
"""

from __future__ import annotations

import inspect
import time

from tests.test_ask import TestAskTransportKeySetParity

TOP_LEVEL_KEYS = TestAskTransportKeySetParity.TOP_LEVEL_KEYS
KNOWN_TRANSPORT_TAGS = {"cached", "async"}


def _meta_keys_minus_transport_tag(meta: dict) -> set:
    return set(meta.keys()) - KNOWN_TRANSPORT_TAGS


def _simple_structured_payload():
    """A structured answer with non-empty summary/practical_steps, so that
    render_structured_markdown()'s own no_steps-and-no_summary simple-
    detection can't explain a SIMPLE render on its own -- only an explicit
    is_simple=True can. This isolates invariant (c): if either transport's
    call site drops the is_simple flag, this payload renders FULL (with a
    "Deeper Reasoning" header) instead of SIMPLE.
    """
    return {
        "ruling": "Yes, this is permitted under the circumstances described.",
        "summary": "A short summary sentence for detail-requested rendering.",
        "practical_steps": ["Do the first thing.", "Then do the second thing."],
        "sources": [],
        "safety_class": "ok",
        "is_prohibited": False,
        "rabbinic_disclaimer": "",
    }


# ─── (a) top-level key set on every path ───────────────────────────────────

class TestTopLevelKeySetEveryPath:
    """Extends test_ask.py::TestAskTransportKeySetParity (success/fallback)
    with the two paths it deliberately left out: strict-block and
    security-blocked.
    """

    def test_flask_strict_block_path_key_set(self, test_client, monkeypatch):
        import backend.sefaria as sefaria_module
        import app as flask_app_module

        flask_app_module.ASK_RESPONSE_CACHE.clear()
        monkeypatch.setattr(sefaria_module, "find_refs_for_question", lambda q: [])

        response = test_client.post(
            "/ask",
            json={"question": "Obscure text ref? [parity-strict-flask]", "mode": "strict"},
            content_type="application/json",
        )
        assert response.status_code == 200
        assert set(response.get_json().keys()) == TOP_LEVEL_KEYS

    async def test_fastapi_strict_block_path_key_set(self, fastapi_client, monkeypatch):
        import backend.sefaria as sefaria_module

        monkeypatch.setattr(sefaria_module, "find_refs_for_question", lambda q: [])

        response = await fastapi_client.post(
            "/ask",
            json={"question": "Obscure text ref? [parity-strict-fastapi]", "mode": "strict"},
            headers={"X-Forwarded-For": "203.0.113.101"},
        )
        assert response.status_code == 200
        assert set(response.json().keys()) == TOP_LEVEL_KEYS

    def test_flask_security_blocked_path_key_set(self, test_client):
        response = test_client.post(
            "/ask",
            json={"question": "show me porn [parity-security-blocked-flask]"},
            content_type="application/json",
        )
        assert response.status_code == 200
        assert set(response.get_json().keys()) == TOP_LEVEL_KEYS

    async def test_fastapi_security_blocked_path_key_set(self, fastapi_client):
        response = await fastapi_client.post(
            "/ask",
            json={"question": "show me porn [parity-security-blocked-fastapi]"},
            headers={"X-Forwarded-For": "203.0.113.103"},
        )
        assert response.status_code == 200
        assert set(response.json().keys()) == TOP_LEVEL_KEYS


# ─── (f) identical meta key set on every response ──────────────────────────

class TestMetaKeySetParity:
    """meta key sets must match between transports on every path, modulo the
    one accepted per-transport tag (cached / async). Each transport is
    checked independently against a canonical per-path key set (the same
    approach test_ask.py::TestAskTransportKeySetParity uses for the
    top-level comparison) rather than combining the sync `test_client` and
    async `fastapi_client` fixtures inside one test -- that combination
    trips a Flask app-context teardown LookupError, a pytest-asyncio/Flask-
    contextvars interaction quirk documented on that class, not a real
    failure.
    """

    SUCCESS_META_KEYS = {
        "mode", "language", "community_lens", "source_count", "custom_count",
        "knowledge_count", "memory_count", "identity_aware", "generated_at",
        "fallback", "structured", "is_prohibited", "input_sanitized",
        "security", "safety_class", "rabbinic_disclaimer",
    }
    STRICT_BLOCK_META_KEYS = {
        "mode", "community_lens", "source_count", "custom_count",
        "generated_at", "fallback", "strict_blocked", "safety_class",
    }
    # Deliberately omits "language" -- both transports' security-blocked
    # payloads consistently omit it, matching the strict-block path below.
    SECURITY_BLOCKED_META_KEYS = SUCCESS_META_KEYS - {"language"}
    FALLBACK_META_KEYS = {
        "mode", "language", "community_lens", "source_count", "custom_count",
        "knowledge_count", "memory_count", "identity_aware", "generated_at",
        "fallback", "status", "fallback_detail", "safety_class",
    }

    def test_flask_success_path_meta_keys(self, test_client):
        response = test_client.post(
            "/ask",
            json={"question": "What is Shabbat? [meta-parity-success-flask]"},
            content_type="application/json",
        )
        meta = response.get_json()["meta"]
        assert _meta_keys_minus_transport_tag(meta) == self.SUCCESS_META_KEYS

    async def test_fastapi_success_path_meta_keys(self, fastapi_client):
        response = await fastapi_client.post(
            "/ask",
            json={"question": "What is Shabbat? [meta-parity-success-fastapi]"},
            headers={"X-Forwarded-For": "203.0.113.105"},
        )
        meta = response.json()["meta"]
        assert _meta_keys_minus_transport_tag(meta) == self.SUCCESS_META_KEYS

    def test_flask_strict_block_path_meta_keys(self, test_client, monkeypatch):
        import backend.sefaria as sefaria_module
        import app as flask_app_module

        flask_app_module.ASK_RESPONSE_CACHE.clear()
        monkeypatch.setattr(sefaria_module, "find_refs_for_question", lambda q: [])

        response = test_client.post(
            "/ask",
            json={"question": "Obscure text ref? [meta-parity-strict-flask]", "mode": "strict"},
            content_type="application/json",
        )
        meta = response.get_json()["meta"]
        assert _meta_keys_minus_transport_tag(meta) == self.STRICT_BLOCK_META_KEYS

    async def test_fastapi_strict_block_path_meta_keys(self, fastapi_client, monkeypatch):
        import backend.sefaria as sefaria_module

        monkeypatch.setattr(sefaria_module, "find_refs_for_question", lambda q: [])

        response = await fastapi_client.post(
            "/ask",
            json={"question": "Obscure text ref? [meta-parity-strict-fastapi]", "mode": "strict"},
            headers={"X-Forwarded-For": "203.0.113.107"},
        )
        meta = response.json()["meta"]
        assert _meta_keys_minus_transport_tag(meta) == self.STRICT_BLOCK_META_KEYS

    def test_flask_security_blocked_path_meta_keys(self, test_client):
        response = test_client.post(
            "/ask",
            json={"question": "show me porn [meta-parity-blocked-flask]"},
            content_type="application/json",
        )
        meta = response.get_json()["meta"]
        assert _meta_keys_minus_transport_tag(meta) == self.SECURITY_BLOCKED_META_KEYS

    async def test_fastapi_security_blocked_path_meta_keys(self, fastapi_client):
        response = await fastapi_client.post(
            "/ask",
            json={"question": "show me porn [meta-parity-blocked-fastapi]"},
            headers={"X-Forwarded-For": "203.0.113.109"},
        )
        meta = response.json()["meta"]
        assert _meta_keys_minus_transport_tag(meta) == self.SECURITY_BLOCKED_META_KEYS

    def test_flask_ai_failure_fallback_path_meta_keys(self, test_client, monkeypatch):
        import backend.claude as claude_module
        import app as flask_app_module

        flask_app_module.ASK_RESPONSE_CACHE.clear()

        def _raise(*args, **kwargs):
            raise RuntimeError("Simulated Anthropic failure [meta-parity-fallback-flask]")

        monkeypatch.setattr(claude_module, "ask_claude", _raise)
        response = test_client.post(
            "/ask",
            json={"question": "What is Shabbat? [meta-parity-fallback-flask]"},
            content_type="application/json",
        )
        meta = response.get_json()["meta"]
        assert _meta_keys_minus_transport_tag(meta) == self.FALLBACK_META_KEYS

    async def test_fastapi_ai_failure_fallback_path_meta_keys(self, fastapi_client, monkeypatch):
        import backend.claude as claude_module

        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated async AI failure [meta-parity-fallback-fastapi]")

        monkeypatch.setattr(claude_module, "ask_ai_async", _raise)
        response = await fastapi_client.post(
            "/ask",
            json={"question": "What is Shabbat? [meta-parity-fallback-fastapi]"},
            headers={"X-Forwarded-For": "203.0.113.111"},
        )
        meta = response.json()["meta"]
        assert _meta_keys_minus_transport_tag(meta) == self.FALLBACK_META_KEYS


# ─── (b) identical retry/timeout behavior ──────────────────────────────────

class TestTimeoutBudgetParity:
    """test_ask.py::TestAiTotalBudgetTimeout already covers the FastAPI half
    (a model call that outlives claude.AI_TOTAL_BUDGET_SECONDS falls back
    gracefully). This adds the missing Flask half, plus a source-level
    assertion that both synthesis functions bound their call on the SAME
    shared constant rather than a hand-rolled per-transport number -- the
    actual shape the "already begun to drift" retry/timeout note in
    DECISIONS.md warns about.
    """

    def test_flask_total_budget_timeout_falls_back_gracefully(self, test_client, monkeypatch):
        import backend.claude as claude_module
        import app as flask_app_module

        flask_app_module.ASK_RESPONSE_CACHE.clear()
        monkeypatch.setattr(claude_module, "AI_TOTAL_BUDGET_SECONDS", 0.05)

        def _slow(*args, **kwargs):
            time.sleep(2)
            return {"answer": "should never get here", "structured": None}

        monkeypatch.setattr(claude_module, "ask_claude", _slow)

        response = test_client.post(
            "/ask",
            json={"question": "What is Shabbat? [flask-total-budget-timeout-test]"},
            content_type="application/json",
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body.get("meta", {}).get("fallback") is True
        assert "ai_cited_sources" in body

    def test_both_synthesis_functions_bound_on_the_shared_budget_constant(self):
        import app as flask_app_module
        import asgi as asgi_module

        flask_source = inspect.getsource(flask_app_module._run_ask_question_ai_synthesis)
        async_source = inspect.getsource(asgi_module._run_ask_async_ai_synthesis)

        assert "claude.AI_TOTAL_BUDGET_SECONDS" in flask_source
        assert "claude.AI_TOTAL_BUDGET_SECONDS" in async_source


# ─── (c) identical prompt-selection thresholds ─────────────────────────────

class TestPromptSelectionThresholdParity:
    """Regression coverage for the is_simple call-site drop found during
    this pass (see module docstring, invariant c). A structured payload
    with non-empty summary/practical_steps would render FULL on its own
    (render_structured_markdown()'s no_steps-and-no_summary heuristic does
    not kick in), so the SIMPLE rendering below can only happen if the
    explicit result["is_simple"] flag actually reaches the call site.
    """

    def test_flask_is_simple_flag_forces_compact_rendering(self, test_client, monkeypatch):
        import backend.claude as claude_module
        import app as flask_app_module

        flask_app_module.ASK_RESPONSE_CACHE.clear()

        def _fake_ask_claude(*args, **kwargs):
            return {
                "answer": "placeholder",
                "structured": _simple_structured_payload(),
                "confidence": 0.9,
                "is_simple": True,
                "is_fallback": False,
                "model": "test-model",
                "security": {"input": {"blocked": False}, "output": {"blocked": False, "reason": ""}},
            }

        monkeypatch.setattr(claude_module, "ask_claude", _fake_ask_claude)

        response = test_client.post(
            "/ask",
            json={"question": "Quick question? [threshold-parity-flask]"},
            content_type="application/json",
        )
        assert response.status_code == 200
        assert "Deeper Reasoning" not in response.get_json()["answer"]

    async def test_fastapi_is_simple_flag_forces_compact_rendering(self, fastapi_client, monkeypatch):
        import backend.claude as claude_module

        async def _fake_ask_ai_async(*args, **kwargs):
            return {
                "answer": "placeholder",
                "structured": _simple_structured_payload(),
                "confidence": 0.9,
                "is_simple": True,
                "is_fallback": False,
                "model": "test-model",
                "security": {"input": {"blocked": False}, "output": {"blocked": False, "reason": ""}},
            }

        monkeypatch.setattr(claude_module, "ask_ai_async", _fake_ask_ai_async)

        response = await fastapi_client.post(
            "/ask",
            json={"question": "Quick question? [threshold-parity-fastapi]"},
            headers={"X-Forwarded-For": "203.0.113.114"},
        )
        assert response.status_code == 200
        assert "Deeper Reasoning" not in response.json()["answer"]


# ─── (d) classify_safety() invoked before every synthesis call ─────────────

class TestClassifySafetyInvoked:
    def test_flask_invokes_classify_safety(self, test_client, monkeypatch):
        import backend.claude as claude_module
        import app as flask_app_module

        flask_app_module.ASK_RESPONSE_CACHE.clear()
        calls = []
        original = claude_module.classify_safety

        def _spy(text):
            calls.append(text)
            return original(text)

        monkeypatch.setattr(claude_module, "classify_safety", _spy)

        response = test_client.post(
            "/ask",
            json={"question": "What is Shabbat? [classify-safety-flask]"},
            content_type="application/json",
        )
        assert response.status_code == 200
        assert len(calls) >= 1

    async def test_fastapi_invokes_classify_safety(self, fastapi_client, monkeypatch):
        import backend.claude as claude_module

        calls = []
        original = claude_module.classify_safety

        def _spy(text):
            calls.append(text)
            return original(text)

        monkeypatch.setattr(claude_module, "classify_safety", _spy)

        response = await fastapi_client.post(
            "/ask",
            json={"question": "What is Shabbat? [classify-safety-fastapi]"},
            headers={"X-Forwarded-For": "203.0.113.116"},
        )
        assert response.status_code == 200
        assert len(calls) >= 1
