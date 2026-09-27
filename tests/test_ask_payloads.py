"""Exact-shape tests for the /ask response builders shared by app.py (Flask)
and asgi.py (FastAPI). The dictionaries are the wire format, so the key set,
key ORDER and the transport-specific trailing keys are all pinned."""

from __future__ import annotations

import pytest

from backend import ask_payloads, claude

FROZEN_NOW = 1_700_000_000.0


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    monkeypatch.setattr(ask_payloads.time, "time", lambda: FROZEN_NOW)


@pytest.fixture
def ctx():
    return {
        "wiki_list": [{"w": 1}], "halachipedia_list": [{"h": 1}],
        "customs_info": [{"c": 1}, {"c": 2}], "primary_sources": [1, 2, 3],
        "knowledge_rows": [1, 2], "user_memory_summaries": ["m"],
    }


def _ai(ctx, **overrides):
    kwargs = dict(
        result={"confidence": 0.9, "is_fallback": False, "security": {"input": {"ok": 1}}},
        answer="the answer", sources=[{"s": 1}], ai_cited=["Shulchan Arukh"],
        structured_payload={"ruling": "r"}, ctx=ctx, mode="strict", answer_language="he",
        canonical_lens="Sefardic", user_id="user_1", question_was_sanitized=True,
        extra_meta={"async": True},
    )
    kwargs.update(overrides)
    return ask_payloads.build_ai_answer_payload(**kwargs)


def _fallback(ctx, **overrides):
    kwargs = dict(
        answer="fallback answer", sources=[{"n": "a"}],
        discovery={"source_count": 4, "status": "partial", "keywords": ["k"], "sequence": ["s"],
                   "counts": {"web": 2}, "fallback_level": "web"},
        warning="General web results.", ai_error=RuntimeError("upstream call timeout"), ctx=ctx,
        mode="balanced", answer_language="en", canonical_lens="All", user_id=None,
        extra_meta={"cached": False},
    )
    kwargs.update(overrides)
    return ask_payloads.build_source_fallback_payload(**kwargs)


class TestAiAnswerPayload:
    def test_full_shape(self, ctx):
        payload = _ai(ctx, structured_payload={
            "ruling": "r", "safety_class": "sensitive_intimate", "is_prohibited": True,
            "rabbinic_disclaimer": "Ask your rabbi."})

        assert payload == {
            "answer": "the answer",
            "confidence": 0.9,
            "wiki": [{"w": 1}, {"h": 1}],
            "customs": [{"c": 1}, {"c": 2}],
            "sources": [{"s": 1}],
            "ai_cited_sources": ["Shulchan Arukh"],
            "history_id": None,
            "meta": {
                "mode": "strict", "language": "he", "community_lens": "Sefardic",
                "source_count": 3, "custom_count": 2, "knowledge_count": 2, "memory_count": 1,
                "identity_aware": True, "generated_at": 1_700_000_000, "fallback": False,
                "structured": True, "is_prohibited": True, "input_sanitized": True,
                "security": {"input": {"ok": 1}}, "safety_class": "sensitive_intimate",
                "rabbinic_disclaimer": "Ask your rabbi.", "async": True,
            },
        }

    def test_meta_key_order_is_the_wire_order_with_transport_keys_last(self, ctx):
        assert list(_ai(ctx)["meta"]) == [
            "mode", "language", "community_lens", "source_count", "custom_count", "knowledge_count",
            "memory_count", "identity_aware", "generated_at", "fallback", "structured",
            "is_prohibited", "input_sanitized", "security", "safety_class", "rabbinic_disclaimer",
            "async",
        ]

    def test_flask_transport_key(self, ctx):
        meta = _ai(ctx, extra_meta={"cached": False})["meta"]

        assert meta["cached"] is False and "async" not in meta

    def test_defaults_without_a_structured_payload(self, ctx):
        payload = _ai(ctx, structured_payload=None, user_id=None, question_was_sanitized=False,
                      result={"confidence": None, "is_fallback": True})

        meta = payload["meta"]
        assert payload["confidence"] is None
        assert (meta["structured"], meta["is_prohibited"], meta["safety_class"]) == (False, False, "ok")
        assert meta["identity_aware"] is False and meta["input_sanitized"] is False
        assert meta["fallback"] is True
        assert meta["security"] == {}
        assert meta["rabbinic_disclaimer"] == claude.RABBI_FINAL_RULING_FOOTER

    def test_a_structured_payload_without_a_disclaimer_uses_the_standard_footer(self, ctx):
        meta = _ai(ctx, structured_payload={"ruling": "r"})["meta"]

        assert meta["structured"] is True
        assert meta["rabbinic_disclaimer"] == claude.RABBI_FINAL_RULING_FOOTER

    def test_an_empty_structured_dict_is_not_reported_as_structured(self, ctx):
        assert _ai(ctx, structured_payload={})["meta"]["structured"] is False

    def test_history_id_defaults_to_none_and_is_forwarded_when_given(self, ctx):
        assert _ai(ctx)["history_id"] is None
        assert _ai(ctx, history_id="row-123")["history_id"] == "row-123"

    def test_the_input_dictionaries_are_not_mutated(self, ctx):
        before = {key: list(value) for key, value in ctx.items()}

        _ai(ctx)

        assert ctx == before


class TestSourceFallbackPayload:
    def test_full_shape(self, ctx):
        payload = _fallback(ctx)

        assert payload == {
            "answer": "fallback answer",
            "confidence": 0.4,
            "wiki": [{"w": 1}, {"h": 1}],
            "customs": [{"c": 1}, {"c": 2}],
            "sources": [{"n": "a"}],
            "ai_cited_sources": [],
            "history_id": None,
            "meta": {
                "mode": "balanced", "language": "en", "community_lens": "All",
                "source_count": 4, "custom_count": 2, "knowledge_count": 2, "memory_count": 1,
                "identity_aware": False, "generated_at": 1_700_000_000, "fallback": True,
                "status": "partial",
                "fallback_detail": {
                    "keywords": ["k"], "sequence": ["s"], "counts": {"web": 2}, "level": "web",
                    "warning": "General web results.", "reason": "timeout",
                },
                "safety_class": "ok", "cached": False,
            },
        }

    def test_meta_key_order_is_the_wire_order_with_transport_keys_last(self, ctx):
        assert list(_fallback(ctx, extra_meta={"async": True})["meta"]) == [
            "mode", "language", "community_lens", "source_count", "custom_count", "knowledge_count",
            "memory_count", "identity_aware", "generated_at", "fallback", "status", "fallback_detail",
            "safety_class", "async",
        ]

    def test_defaults_when_discovery_returned_nothing(self, ctx):
        meta = _fallback(ctx, discovery={}, warning="", user_id="u1")["meta"]

        assert meta["source_count"] == 0
        assert meta["status"] == "fallback"
        assert meta["identity_aware"] is True
        assert meta["fallback_detail"] == {
            "keywords": [], "sequence": [], "counts": {}, "level": "unknown",
            "warning": "", "reason": "timeout",
        }

    @pytest.mark.parametrize("error, reason", [
        (RuntimeError("upstream call timeout"), "timeout"),
        (RuntimeError("anthropic_sdk_error: 429 rate limit exceeded"), "rate_limited"),
        (RuntimeError("security_blocked: disallowed content"), "blocked"),
        (RuntimeError("sk-ant-SECRET exploded"), "provider_error"),
    ])
    def test_only_a_coarse_reason_is_exposed_never_the_error_text(self, ctx, error, reason):
        meta = _fallback(ctx, ai_error=error)["meta"]

        assert meta["fallback_detail"]["reason"] == reason
        assert "SECRET" not in repr(meta)
