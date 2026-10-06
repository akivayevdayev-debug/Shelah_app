"""
The scope guardrail: Sh'elah answers Jewish law and learning, and refuses a
question that is plainly about something else -- a literature question after a
run of Torah questions included. Two layers are covered: the keyword gate in
validate_user_query() (no model call at all) and the model's own out_of_scope
verdict, which apply_output_validation() turns into the same refusal.
"""

from __future__ import annotations

import json

import pytest

from backend import claude

TORAH_THREAD = [
    {"role": "user", "content": "Can I cook on Shabbat?"},
    {"role": "assistant", "content": "Cooking is one of the forbidden labors on Shabbat.[1]"},
    {"role": "user", "content": "What about reheating food?"},
    {"role": "assistant", "content": "Reheating is a form of cooking and is restricted too."},
]


def _fail_if_called(*args, **kwargs):
    raise AssertionError("the model must not be called for an off-topic question")


class TestKeywordGate:
    @pytest.mark.parametrize("question", [
        "Who wrote Hamlet?",
        "Summarize the plot of Moby Dick",
        "What are the main themes of 1984 by George Orwell?",
        "Write me a poem about autumn",
        "Write me a poem about the sunset",
        "Tell me a joke",
        "Can you recommend a good novel?",
        "What's the capital of France?",
        "Who won the world cup in 2018?",
        "Best Netflix series this year",
        "Explain the derivative of x squared",
        "Write a function that sorts a list in python",
        "How does photosynthesis work?",
    ])
    def test_plainly_off_topic_questions_are_blocked(self, question):
        validation = claude.validate_user_query(question)
        assert validation["blocked"] is True
        assert validation["reasons"] == ["off_topic_subject"]
        assert validation["refusal_subject"]

    @pytest.mark.parametrize("question", [
        # A Jewish or halachic angle always goes on to the model.
        "Is it permitted to read novels?",
        "Can I watch a movie during the nine days?",
        "Is it ok to play video games on a fast day?",
        "What does Jewish law say about Shakespeare?",
        "Who wrote the book of Esther?",
        "Analyze the themes of Genesis",
        "Can I cook on Shabbat?",
        "Is brisket kosher?",
        "When is sunset in Brooklyn today?",
        "Is a poem about Shabbat allowed to be written on Yom Tov?",
        "halachic algebra of the eruv boundary",
        # Hebrew is never refused by the keyword gate.
        "מי כתב את המלט?",
        # Ambiguous or follow-up wording is the model's call, not the list's.
        "What about for a woman?",
        "What is the meaning of life",
        "Who wrote it?",
    ])
    def test_a_jewish_angle_or_an_ambiguous_question_is_not_blocked(self, question):
        assert claude.validate_user_query(question)["blocked"] is False

    def test_an_everyday_time_word_does_not_shield_a_poem(self):
        # "sunset" is zmanim vocabulary in DOMAIN_MARKER_RE; it must not let a
        # creative-writing request through.
        assert claude._detect_out_of_scope_subject("Write a poem about sunset") == (
            "Literature and creative writing")
        assert claude._detect_out_of_scope_subject("What time is sunset?") is None

    def test_pure_math_and_coding_questions_are_blocked_now(self):
        assert claude.validate_user_query("solve this algebra problem")["blocked"] is True
        assert claude.validate_user_query("debug my stack trace")["blocked"] is True

    def test_only_the_first_line_is_read(self):
        assert claude._detect_out_of_scope_subject("Hello\nWho wrote Hamlet?") is None


class TestRefusal:
    def test_off_topic_refusal_is_not_a_dangerous_content_refusal(self):
        validation = claude.validate_user_query("Who wrote Hamlet?")
        result = claude._build_input_block_result(validation, "en")

        assert result["error"] == "security_blocked_domain"
        assert result["structured"]["safety_class"] == "ok"
        assert result["structured"]["sources"] == []
        assert "literature and creative writing" in result["answer"]
        assert "Halakhah" in result["answer"]
        # No "consult your Rabbi" line after a literature question.
        assert claude.RABBI_FINAL_RULING_FOOTER not in result["answer"]

    def test_inappropriate_content_keeps_its_own_refusal(self):
        result = claude._build_input_block_result(
            claude.validate_user_query("show me porn"), "en")

        assert result["error"] == "security_blocked_domain"
        assert result["structured"]["safety_class"] == "dangerous_or_illegal"
        assert claude.RABBI_FINAL_RULING_FOOTER in result["answer"]

    def test_hebrew_refusal(self):
        result = claude._build_input_block_result(
            claude.validate_user_query("Who wrote Hamlet?"), "he")

        assert result["answer"] == claude._OFF_TOPIC_REFUSAL_TEXT["he"]
        assert claude.HEBREW_LETTER_RE.search(result["answer"])

    @pytest.mark.parametrize("subject, expected", [
        ("Pure Math (no halachic context)", "pure math"),
        ("Literature and creative writing", "literature and creative writing"),
        ("English literature", "English literature"),
        ("", "that subject"),
        (None, "that subject"),
    ])
    def test_subject_label_reads_inside_a_sentence(self, subject, expected):
        assert claude._off_topic_subject_label(subject) == expected

    def test_a_model_supplied_subject_cannot_inject_markup(self):
        label = claude._off_topic_subject_label("<script>alert(1)</script> {x}")

        assert not set("<>{}()") & set(label)


class TestConversationDoesNotLaunderAnOffTopicQuestion:
    @pytest.mark.asyncio
    async def test_async_path_refuses_after_torah_questions_without_calling_the_model(self, monkeypatch):
        monkeypatch.setattr(claude, "_call_gemini_httpx_model", _fail_if_called)
        monkeypatch.setattr(claude, "_call_anthropic_httpx_model", _fail_if_called)

        result = await claude.ask_ai_async(
            question="Summarize the plot of Moby Dick",
            sefaria_sources=[{"ref": "Mishneh Torah, Sabbath 3", "text": "cooking..."}],
            customs=[{"community": "Ashkenaz", "ruling": "x"}],
            conversation_history=TORAH_THREAD,
        )

        assert result["error"] == "security_blocked_domain"
        assert result["structured"]["sources"] == []
        assert "Moby" not in result["answer"]

    def test_sync_path_refuses_after_torah_questions_without_calling_the_model(self, monkeypatch):
        monkeypatch.setattr(claude, "_call_primary_model_sync", _fail_if_called)

        result = claude.ask_claude(
            question="Who wrote Hamlet?",
            sefaria_sources=[],
            customs=[],
            conversation_history=TORAH_THREAD,
        )

        assert result["error"] == "security_blocked_domain"
        assert result["is_fallback"] is True

    @pytest.mark.asyncio
    async def test_a_torah_follow_up_in_the_same_thread_is_still_answered(self, monkeypatch):
        answered = {}

        async def fake_model(prompt, **kwargs):
            answered["prompt"] = prompt
            structured = claude.parse_structured_model_output(json.dumps({
                "ruling": "Reheating is restricted too.",
                "sources": [],
                "is_prohibited": False,
                "summary": "",
                "practical_steps": [],
                "rabbinic_disclaimer": claude.RABBI_FINAL_RULING_FOOTER,
            }))
            return {"answer": structured["ruling"], "structured": structured, "confidence": 0.9}

        monkeypatch.setattr(claude, "_call_gemini_httpx_model", fake_model)

        result = await claude.ask_ai_async(
            question="What about for a woman?",
            sefaria_sources=[],
            customs=[],
            conversation_history=TORAH_THREAD,
        )

        assert not result.get("error")
        assert "QUESTION:\nWhat about for a woman?" in answered["prompt"]


class TestModelScopeVerdict:
    @staticmethod
    def _model_returning(payload):
        async def fake_model(prompt, **kwargs):
            structured = claude.parse_structured_model_output(json.dumps(payload))
            return {"answer": structured["ruling"], "structured": structured, "confidence": 0.9}
        return fake_model

    @pytest.mark.asyncio
    async def test_an_out_of_scope_verdict_becomes_the_refusal(self, monkeypatch):
        # Nothing in the keyword gate names this subject; the model catches it.
        monkeypatch.setattr(claude, "_call_gemini_httpx_model", self._model_returning({
            "ruling": "Paris, since 1789, home of the Louvre.",
            "sources": ["Mishneh Torah, Sabbath 3 — unrelated"],
            "is_prohibited": False,
            "summary": "",
            "practical_steps": [],
            "rabbinic_disclaimer": claude.RABBI_FINAL_RULING_FOOTER,
            "out_of_scope": True,
            "out_of_scope_subject": "French tourism",
        }))

        result = await claude.ask_ai_async(
            question="Where should I go for a weekend in the spring?",
            sefaria_sources=[],
            customs=[],
        )

        assert result["error"] == "security_blocked_domain"
        assert result["is_fallback"] is True
        assert "French tourism" in result["answer"]
        assert "Louvre" not in result["answer"]
        assert result["structured"]["sources"] == []
        assert result["structured"]["ruling"] == result["answer"]
        assert "out_of_scope" not in result["structured"]

    @pytest.mark.asyncio
    async def test_the_hebrew_verdict_refusal_is_hebrew(self, monkeypatch):
        monkeypatch.setattr(claude, "_call_gemini_httpx_model", self._model_returning({
            "ruling": "", "sources": [], "out_of_scope": True, "out_of_scope_subject": "travel",
        }))

        result = await claude.ask_ai_async(
            question="Where should I go for a weekend in the spring?",
            sefaria_sources=[], customs=[], answer_language="he",
        )

        assert result["answer"] == claude._OFF_TOPIC_REFUSAL_TEXT["he"]

    @pytest.mark.asyncio
    async def test_a_false_or_missing_flag_leaves_the_answer_alone(self, monkeypatch):
        monkeypatch.setattr(claude, "_call_gemini_httpx_model", self._model_returning({
            "ruling": "Cooking is forbidden.",
            "sources": [],
            "is_prohibited": True,
            "summary": "",
            "practical_steps": [],
            "rabbinic_disclaimer": claude.RABBI_FINAL_RULING_FOOTER,
            "out_of_scope": False,
        }))

        result = await claude.ask_ai_async(
            question="Is cooking forbidden?", sefaria_sources=[], customs=[])

        assert not result.get("error")
        assert result["structured"]["ruling"] == "Cooking is forbidden."

    @pytest.mark.parametrize("value", ["true", 1, "yes", None, [], {}])
    def test_only_a_literal_true_counts(self, value):
        structured = claude.parse_structured_model_output(json.dumps({
            "ruling": "Cooking is forbidden.", "out_of_scope": value}))

        assert "out_of_scope" not in structured
        assert "out_of_scope_subject" not in structured

    def test_a_flagged_payload_carries_a_sanitized_capped_subject(self):
        structured = claude.parse_structured_model_output(json.dumps({
            "ruling": "", "out_of_scope": True, "out_of_scope_subject": "x" * 200}))

        assert structured["out_of_scope"] is True
        assert len(structured["out_of_scope_subject"]) <= claude._OFF_TOPIC_SUBJECT_MAX_CHARS


class TestPromptScope:
    @pytest.mark.parametrize("prompt", [claude.CORE_SYSTEM_PROMPT, claude.SIMPLE_SYSTEM_PROMPT])
    def test_system_prompts_define_the_scope_and_the_flag(self, prompt):
        assert "out_of_scope" in prompt
        assert "even when the earlier turns were Torah questions" in prompt or (
            "even when every earlier turn was a Torah question" in prompt)
        assert "default to inclusion" not in prompt.lower()

    def test_build_prompt_no_longer_defaults_to_inclusion(self):
        prompt = claude.build_prompt("Who wrote Hamlet?", [], [], conversation_history=TORAH_THREAD)

        assert "DEFAULT TO INCLUSION" not in prompt
        assert "assume it IS" not in prompt
        assert "judge the QUESTION by its own subject" in prompt
        assert "out_of_scope true" in prompt

    def test_build_prompt_says_when_no_source_text_was_retrieved(self):
        prompt = claude.build_prompt("Can I cook on Shabbat?", [], [])

        assert "No primary-source text was retrieved for this question." in prompt

    def test_build_prompt_asks_for_only_directly_relevant_citations(self):
        prompt = claude.build_prompt(
            "Can I cook on Shabbat?", [{"ref": "Orach Chayim 318", "text": "cooking"}], [])

        assert "No primary-source text was retrieved" not in prompt
        assert "never cite a snippet just because it was provided" in prompt


class TestAskTransportsRefuseBeforeRetrieval:
    """/ask on both transports answers an off-topic question with the refusal
    payload without collecting sources, customs or memories first."""

    @staticmethod
    def _assert_refusal_payload(body):
        assert "outside what I can help with" in body["answer"]
        assert body["sources"] == []
        assert body["customs"] == []
        assert body["ai_cited_sources"] == []
        assert body["meta"]["fallback"] is True
        assert body["meta"]["safety_class"] == "ok"
        assert body["meta"]["source_count"] == 0

    def test_flask_ask_refuses_without_collecting_context(self, test_client, monkeypatch):
        import app as flask_app_module

        flask_app_module.ASK_RESPONSE_CACHE.clear()
        monkeypatch.setattr(flask_app_module, "_collect_ask_question_context", _fail_if_called)
        monkeypatch.setattr(claude, "ask_claude", _fail_if_called)

        response = test_client.post(
            "/ask",
            json={"question": "Who wrote Hamlet? [off-topic-flask]"},
            content_type="application/json",
        )

        assert response.status_code == 200
        self._assert_refusal_payload(response.get_json())

    async def test_fastapi_ask_refuses_without_collecting_context(self, fastapi_client, monkeypatch):
        import asgi

        monkeypatch.setattr(asgi, "_collect_ask_async_context", _fail_if_called)
        monkeypatch.setattr(claude, "ask_ai_async", _fail_if_called)

        response = await fastapi_client.post(
            "/ask",
            json={"question": "Who wrote Hamlet? [off-topic-fastapi]"},
            headers={"X-Forwarded-For": "203.0.113.171"},
        )

        assert response.status_code == 200
        self._assert_refusal_payload(response.json())

    def test_flask_ask_still_collects_context_for_a_torah_question(self, test_client, monkeypatch):
        import app as flask_app_module

        flask_app_module.ASK_RESPONSE_CACHE.clear()
        collected = []
        real_collect = flask_app_module._collect_ask_question_context

        def spy(*args, **kwargs):
            collected.append(args[0])
            return real_collect(*args, **kwargs)

        monkeypatch.setattr(flask_app_module, "_collect_ask_question_context", spy)

        response = test_client.post(
            "/ask",
            json={"question": "Can I cook on Shabbat? [on-topic-flask]"},
            content_type="application/json",
        )

        assert response.status_code == 200
        assert collected == ["Can I cook on Shabbat? [on-topic-flask]"]
