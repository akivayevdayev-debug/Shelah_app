"""
Behavioural coverage for the branches of asgi.py that the end-to-end /ask suite
(tests/test_ask*.py) and the pure-helper suite (tests/test_asgi_helpers.py) do
not reach:

  * _collect_primary_sources -- per-ref load failures are swallowed + logged
  * _build_tool_context -- builder failure degrades to the default context
  * request_id_middleware -- the Content-Length body cap (413) and its
    unparseable-header tolerance
  * _security_blocked_ask_async_payload -- non-dict structured payload and
    empty-answer fallbacks
  * _enforce_ask_async_auth_required / _enforce_ask_async_budget /
    _resolve_ask_async_request_params -- 401, 402 and language normalisation
  * ask_async's outer try/except -- HTTPException pass-through vs. Sentry
    capture + generic 500 for anything else

Every network / LLM / Supabase dependency is stubbed with unittest.mock.
"""

import logging
from unittest import mock

import pytest
from fastapi import HTTPException

import app as flask_app_module
import asgi
from backend import claude


@pytest.fixture(autouse=True)
def _allow_budget(monkeypatch):
    """Keep the /ask route tests hermetic: never consult the real spend meter
    (tests that care about the budget install their own stub afterwards)."""
    monkeypatch.setattr(
        asgi, "check_user_budget_and_enforce",
        mock.AsyncMock(return_value={"allowed": True, "total_usd": 0.0, "threshold_usd": 1.0}),
    )


def _ctx(knowledge=0, memories=0):
    return {
        "knowledge_rows": [{}] * knowledge,
        "user_memory_summaries": [{}] * memories,
    }


# -- _collect_primary_sources ---------------------------------------------------


class _StubEngine:
    """ShelahEngine stand-in: get_library_text(ref) returns or raises whatever
    the per-ref outcome table says."""

    def __init__(self, outcomes):
        self._outcomes = outcomes

    def get_library_text(self, ref):
        outcome = self._outcomes[ref]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


async def test_collect_primary_sources_swallows_and_logs_a_failed_ref_load(monkeypatch, caplog):
    seen_questions = []

    def fake_find_refs(question):
        seen_questions.append(question)
        return ["Good 1", "Bad 2", "Odd 3"]

    monkeypatch.setattr(asgi._backend_sefaria, "find_refs_for_question", fake_find_refs)
    good = {"ref": "Good 1", "lines": [{"en": "text"}]}
    engine = _StubEngine({
        "Good 1": good,
        "Bad 2": RuntimeError("sefaria exploded"),
        "Odd 3": ["not", "a", "dict"],
    })
    monkeypatch.setattr(asgi, "ShelahEngine", lambda: engine)

    with caplog.at_level(logging.DEBUG, logger="asgi"):
        refs, sources = await asgi._collect_primary_sources("may I cook on shabbat?")

    assert seen_questions == ["may I cook on shabbat?"]
    # refs are reported as found; only the successfully-loaded dict survives
    assert refs == ["Good 1", "Bad 2", "Odd 3"]
    assert sources == [good]
    failures = [r for r in caplog.records if "Source load failed" in r.getMessage()]
    assert len(failures) == 1
    assert failures[0].levelno == logging.DEBUG
    assert "'Bad 2'" in failures[0].getMessage()
    assert "sefaria exploded" in failures[0].getMessage()


async def test_collect_primary_sources_treats_non_list_refs_as_no_refs(monkeypatch):
    monkeypatch.setattr(asgi._backend_sefaria, "find_refs_for_question", lambda q: None)
    engine_factory = mock.MagicMock()
    monkeypatch.setattr(asgi, "ShelahEngine", engine_factory)

    assert await asgi._collect_primary_sources("q") == ([], [])
    engine_factory.assert_not_called()


# -- _build_tool_context --------------------------------------------------------


async def test_build_tool_context_returns_builder_dict_for_a_fresh_engine(monkeypatch):
    sentinel_engine = object()
    monkeypatch.setattr(asgi, "ShelahEngine", lambda: sentinel_engine)
    builder = mock.MagicMock(return_value={"route": "/ask", "tools": ["a"]})
    monkeypatch.setattr(asgi, "_build_ask_tool_context", builder)

    assert await asgi._build_tool_context() == {"route": "/ask", "tools": ["a"]}
    builder.assert_called_once_with(sentinel_engine)


async def test_build_tool_context_replaces_non_dict_builder_result(monkeypatch):
    monkeypatch.setattr(asgi, "ShelahEngine", lambda: object())
    monkeypatch.setattr(asgi, "_build_ask_tool_context", lambda engine: ["nope"])

    assert await asgi._build_tool_context() == {"route": "/ask", "async": True}


async def test_build_tool_context_degrades_to_default_and_logs_on_builder_failure(monkeypatch, caplog):
    monkeypatch.setattr(asgi, "ShelahEngine", lambda: object())

    def exploding_builder(engine):
        raise RuntimeError("tool registry offline")

    monkeypatch.setattr(asgi, "_build_ask_tool_context", exploding_builder)

    with caplog.at_level(logging.DEBUG, logger="asgi"):
        context = await asgi._build_tool_context()

    assert context == {"route": "/ask", "async": True}
    failures = [r for r in caplog.records if "Tool context build failed" in r.getMessage()]
    assert len(failures) == 1
    assert failures[0].levelno == logging.DEBUG
    assert "tool registry offline" in failures[0].getMessage()


# -- request_id_middleware: Content-Length cap ----------------------------------


async def test_middleware_rejects_content_length_over_cap_with_413(fastapi_client):
    over_cap = asgi._MAX_ASGI_BODY_BYTES + 1

    response = await fastapi_client.get(
        "/api/async/health",
        headers={"Content-Length": str(over_cap), "X-Forwarded-For": "203.0.113.211"},
    )

    assert response.status_code == 413
    assert response.json() == {"error": "Request body too large."}


async def test_middleware_413_short_circuits_before_the_route_runs(fastapi_client, monkeypatch):
    collect = mock.AsyncMock()
    monkeypatch.setattr(asgi, "_collect_ask_async_context", collect)

    response = await fastapi_client.post(
        "/ask",
        content=b'{"question": "x"}',
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(asgi._MAX_ASGI_BODY_BYTES + 1),
            "X-Forwarded-For": "203.0.113.212",
        },
    )

    assert response.status_code == 413
    collect.assert_not_called()


async def test_middleware_accepts_content_length_exactly_at_cap(fastapi_client):
    response = await fastapi_client.get(
        "/api/async/health",
        headers={
            "Content-Length": str(asgi._MAX_ASGI_BODY_BYTES),
            "X-Forwarded-For": "203.0.113.213",
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True


async def test_middleware_ignores_unparseable_content_length(fastapi_client):
    response = await fastapi_client.get(
        "/api/async/health",
        headers={"Content-Length": "not-a-number", "X-Forwarded-For": "203.0.113.214"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.headers["x-request-id"]


# -- _security_blocked_ask_async_payload ----------------------------------------

DEFAULT_BLOCKED_ANSWER = "Request blocked by security policy. Please submit a direct halakhic question."


async def test_security_blocked_payload_defaults_when_structured_is_not_a_dict(monkeypatch):
    store = mock.MagicMock()
    monkeypatch.setattr(asgi, "_store_ask_history", store)
    result = {"structured": "junk", "answer": "   ", "confidence": 0.3, "security": {"rule": "x"}}

    with mock.patch.dict(flask_app_module.DEVTOOLS_STATS, {"answers_total": 0, "fallback_answers": 0}):
        payload = await asgi._security_blocked_ask_async_payload(
            result, "strict", "Yemenite", "he", "user_1", True, "bad question", _ctx(knowledge=2, memories=1),
        )
        stats = dict(flask_app_module.DEVTOOLS_STATS)

    assert stats["answers_total"] == 1
    assert stats["fallback_answers"] == 1
    assert payload["answer"] == DEFAULT_BLOCKED_ANSWER
    assert payload["confidence"] == 0.3
    assert payload["sources"] == payload["customs"] == payload["wiki"] == payload["ai_cited_sources"] == []
    meta = payload["meta"]
    assert meta["safety_class"] == "ok"
    assert meta["is_prohibited"] is False
    assert meta["rabbinic_disclaimer"] == claude.RABBI_FINAL_RULING_FOOTER
    assert meta["security"] == {"rule": "x"}
    assert meta["fallback"] is True and meta["structured"] is False and meta["async"] is True
    assert meta["input_sanitized"] is True and meta["identity_aware"] is True
    assert (meta["mode"], meta["community_lens"]) == ("strict", "Yemenite")
    assert (meta["knowledge_count"], meta["memory_count"]) == (2, 1)
    store.assert_called_once_with(
        "user_1", "bad question", DEFAULT_BLOCKED_ANSWER,
        sources=[], ai_cited_sources=[], community="Yemenite", mode="strict",
        language="he", safety_class="ok", prompt_version=claude.PROMPT_VERSION,
    )


async def test_security_blocked_payload_defaults_when_answer_is_missing(monkeypatch):
    store = mock.MagicMock()
    monkeypatch.setattr(asgi, "_store_ask_history", store)

    with mock.patch.dict(flask_app_module.DEVTOOLS_STATS, {"answers_total": 0, "fallback_answers": 0}):
        payload = await asgi._security_blocked_ask_async_payload(
            {}, "balanced", "All", "en", None, False, "q", _ctx(),
        )

    assert payload["answer"] == DEFAULT_BLOCKED_ANSWER
    assert payload["confidence"] == 0
    assert payload["meta"]["security"] == {}
    assert payload["meta"]["identity_aware"] is False
    assert store.call_args.args[2] == DEFAULT_BLOCKED_ANSWER


async def test_security_blocked_payload_uses_structured_dict_and_stripped_answer(monkeypatch):
    store = mock.MagicMock()
    monkeypatch.setattr(asgi, "_store_ask_history", store)
    result = {
        "answer": "  Blocked for safety.  ",
        "structured": {
            "safety_class": "medical",
            "is_prohibited": True,
            "rabbinic_disclaimer": "Ask your posek.",
        },
    }

    with mock.patch.dict(flask_app_module.DEVTOOLS_STATS, {"answers_total": 0, "fallback_answers": 0}):
        payload = await asgi._security_blocked_ask_async_payload(
            result, "balanced", "All", "en", "u", False, "q", _ctx(),
        )

    assert payload["answer"] == "Blocked for safety."
    assert payload["meta"]["safety_class"] == "medical"
    assert payload["meta"]["is_prohibited"] is True
    assert payload["meta"]["rabbinic_disclaimer"] == "Ask your posek."
    assert store.call_args.args[2] == "Blocked for safety."
    assert store.call_args.kwargs["safety_class"] == "medical"


# -- _enforce_ask_async_auth_required -------------------------------------------


def test_auth_required_raises_401_when_enforced_and_anonymous(monkeypatch):
    monkeypatch.setattr(asgi, "CLERK_ENFORCE_AUTH", True)

    with pytest.raises(HTTPException) as excinfo:
        asgi._enforce_ask_async_auth_required(None)

    assert excinfo.value.status_code == 401
    assert excinfo.value.detail == "Authentication required"


def test_auth_required_raises_401_for_empty_user_id(monkeypatch):
    monkeypatch.setattr(asgi, "CLERK_ENFORCE_AUTH", True)

    with pytest.raises(HTTPException) as excinfo:
        asgi._enforce_ask_async_auth_required("")

    assert excinfo.value.status_code == 401


def test_auth_required_allows_identified_caller_when_enforced(monkeypatch):
    monkeypatch.setattr(asgi, "CLERK_ENFORCE_AUTH", True)

    assert asgi._enforce_ask_async_auth_required("user_1") is None


def test_auth_required_allows_anonymous_when_not_enforced(monkeypatch):
    monkeypatch.setattr(asgi, "CLERK_ENFORCE_AUTH", False)

    assert asgi._enforce_ask_async_auth_required(None) is None


async def test_ask_route_returns_401_for_anonymous_when_auth_enforced(fastapi_client, monkeypatch):
    monkeypatch.setattr(asgi, "CLERK_ENFORCE_AUTH", True)
    budget = mock.AsyncMock(return_value={"allowed": True, "total_usd": 0, "threshold_usd": 1})
    monkeypatch.setattr(asgi, "check_user_budget_and_enforce", budget)

    response = await fastapi_client.post(
        "/ask",
        json={"question": "What is the law of lighting candles?"},
        headers={"X-Forwarded-For": "203.0.113.221"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required"}
    budget.assert_not_called()


# -- _enforce_ask_async_budget --------------------------------------------------


async def test_budget_check_raises_402_with_formatted_totals(monkeypatch):
    budget = mock.AsyncMock(return_value={"allowed": False, "total_usd": 3.456, "threshold_usd": 5})
    monkeypatch.setattr(asgi, "check_user_budget_and_enforce", budget)

    with pytest.raises(HTTPException) as excinfo:
        await asgi._enforce_ask_async_budget("user_1", "203.0.113.5")

    assert excinfo.value.status_code == 402
    assert excinfo.value.detail == (
        "Daily AI usage limit reached for this account "
        "($3.46 of $5.00). "
        "Please try again after midnight UTC."
    )
    budget.assert_awaited_once_with("user_1", "203.0.113.5")


async def test_budget_check_passes_when_allowed(monkeypatch):
    budget = mock.AsyncMock(return_value={"allowed": True, "total_usd": 9.99, "threshold_usd": 1})
    monkeypatch.setattr(asgi, "check_user_budget_and_enforce", budget)

    assert await asgi._enforce_ask_async_budget(None, "203.0.113.6") is None
    budget.assert_awaited_once_with(None, "203.0.113.6")


async def test_ask_route_returns_402_before_running_the_pipeline(fastapi_client, monkeypatch):
    budget = mock.AsyncMock(return_value={"allowed": False, "total_usd": 2.0, "threshold_usd": 2.0})
    monkeypatch.setattr(asgi, "check_user_budget_and_enforce", budget)
    collect = mock.AsyncMock()
    monkeypatch.setattr(asgi, "_collect_ask_async_context", collect)

    response = await fastapi_client.post(
        "/ask",
        json={"question": "What is the law of lighting candles?"},
        headers={"X-Forwarded-For": "203.0.113.222"},
    )

    assert response.status_code == 402
    assert "$2.00 of $2.00" in response.json()["detail"]
    budget.assert_awaited_once_with(None, "203.0.113.222")
    collect.assert_not_called()


# -- _resolve_ask_async_request_params ------------------------------------------


def _params_payload(**kwargs):
    return asgi.AskRequest(question="q", **kwargs)


def test_request_params_unsupported_language_falls_back_to_english():
    _, _, language = asgi._resolve_ask_async_request_params(_params_payload(language="fr"))

    assert language == "en"


def test_request_params_language_is_trimmed_and_lowercased():
    _, _, language = asgi._resolve_ask_async_request_params(_params_payload(language=" HE "))

    assert language == "he"


def test_request_params_missing_language_defaults_to_english():
    _, _, language = asgi._resolve_ask_async_request_params(_params_payload())

    assert language == "en"


def test_request_params_all_community_is_case_insensitive_and_mode_is_sanitised():
    mode, lens, _ = asgi._resolve_ask_async_request_params(
        _params_payload(mode=" STRICT ", community="aLL"))

    assert (mode, lens) == ("strict", "All")


def test_request_params_unknown_mode_falls_back_to_balanced():
    mode, _, _ = asgi._resolve_ask_async_request_params(_params_payload(mode="bogus"))

    assert mode == "balanced"


def test_request_params_blank_community_defaults_to_all():
    _, lens, _ = asgi._resolve_ask_async_request_params(_params_payload(community="   "))

    assert lens == "All"


def test_request_params_unrecognised_community_is_kept_verbatim(monkeypatch):
    monkeypatch.setattr(asgi, "_canonicalize_community_name", lambda name: None)

    _, lens, _ = asgi._resolve_ask_async_request_params(_params_payload(community="Atlantis"))

    assert lens == "Atlantis"


def test_request_params_recognised_community_is_canonicalised(monkeypatch):
    seen = []

    def fake_canonicalize(name):
        seen.append(name)
        return "Canonical Name"

    monkeypatch.setattr(asgi, "_canonicalize_community_name", fake_canonicalize)

    _, lens, _ = asgi._resolve_ask_async_request_params(_params_payload(community="alias"))

    assert lens == "Canonical Name"
    assert seen == ["alias"]


# -- ask_async: outer try/except ------------------------------------------------

_STAGES = [
    "_ask_async_prayer_result",
    "_collect_ask_async_context",
    "_ask_async_strict_block",
    "_resolve_ask_async_breaker_response",
    "_run_ask_async_synthesis_or_fallback",
]
_ASYNC_STAGES = {
    "_collect_ask_async_context",
    "_resolve_ask_async_breaker_response",
    "_run_ask_async_synthesis_or_fallback",
}


def _stub_stages_failing_at(monkeypatch, failing_stage, exc):
    """Make every pipeline stage before `failing_stage` a benign no-op, and
    `failing_stage` raise `exc`."""
    benign_returns = {
        "_ask_async_prayer_result": None,
        "_collect_ask_async_context": {},
        "_ask_async_strict_block": None,
        "_resolve_ask_async_breaker_response": None,
        "_run_ask_async_synthesis_or_fallback": {"answer": "unused"},
    }
    failing_index = _STAGES.index(failing_stage)
    for index, name in enumerate(_STAGES):
        factory = mock.AsyncMock if name in _ASYNC_STAGES else mock.MagicMock
        if index < failing_index:
            monkeypatch.setattr(asgi, name, factory(return_value=benign_returns[name]))
        elif index == failing_index:
            monkeypatch.setattr(asgi, name, factory(side_effect=exc))
        else:
            monkeypatch.setattr(asgi, name, factory(side_effect=AssertionError(
                f"{name} must not run after {failing_stage} failed")))


@pytest.mark.parametrize("failing_stage", _STAGES)
async def test_ask_route_reports_unexpected_stage_failure_and_returns_generic_500(
    fastapi_client, monkeypatch, failing_stage,
):
    boom = RuntimeError("stage blew up")
    _stub_stages_failing_at(monkeypatch, failing_stage, boom)
    capture = mock.MagicMock()
    monkeypatch.setattr(asgi, "_capture_backend_error", capture)

    response = await fastapi_client.post(
        "/ask",
        json={"question": "What is the law of lighting candles?", "mode": "strict", "community": "all"},
        headers={"X-Forwarded-For": f"203.0.113.{231 + _STAGES.index(failing_stage)}"},
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "An internal error occurred while processing your request."}
    # the raw exception text must never leak to the client
    assert "stage blew up" not in response.text
    capture.assert_called_once()
    label, reported_exc, context = capture.call_args.args
    assert label == "ask_route_critical_error_async"
    assert reported_exc is boom
    assert context == {
        "question": "What is the law of lighting candles?",
        "mode": "strict",
        "community_lens": "All",
    }


async def test_ask_route_lets_http_exceptions_from_a_stage_pass_through_unreported(
    fastapi_client, monkeypatch,
):
    _stub_stages_failing_at(
        monkeypatch, "_collect_ask_async_context",
        HTTPException(status_code=418, detail="teapot"),
    )
    capture = mock.MagicMock()
    monkeypatch.setattr(asgi, "_capture_backend_error", capture)

    response = await fastapi_client.post(
        "/ask",
        json={"question": "What is the law of lighting candles?"},
        headers={"X-Forwarded-For": "203.0.113.241"},
    )

    assert response.status_code == 418
    assert response.json() == {"detail": "teapot"}
    capture.assert_not_called()


async def test_ask_route_returns_the_synthesis_payload_when_no_stage_fails(fastapi_client, monkeypatch):
    for name in _STAGES:
        if name in _ASYNC_STAGES:
            benign = {"_collect_ask_async_context": {},
                      "_resolve_ask_async_breaker_response": None,
                      "_run_ask_async_synthesis_or_fallback": {"answer": "final answer"}}[name]
            monkeypatch.setattr(asgi, name, mock.AsyncMock(return_value=benign))
        else:
            monkeypatch.setattr(asgi, name, mock.MagicMock(return_value=None))
    capture = mock.MagicMock()
    monkeypatch.setattr(asgi, "_capture_backend_error", capture)

    response = await fastapi_client.post(
        "/ask",
        json={"question": "What is the law of lighting candles?"},
        headers={"X-Forwarded-For": "203.0.113.242"},
    )

    assert response.status_code == 200
    assert response.json() == {"answer": "final answer"}
    capture.assert_not_called()
