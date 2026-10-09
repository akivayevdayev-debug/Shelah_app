"""The 2026-10-09 answer-pipeline upgrade: earlier turns sent as real conversation
turns, a prompt-cache layout for the Claude fallback, learning questions given a
sectioned answer with room to finish, cut-off output salvaged instead of shown
half-finished, and a summary/steps that only repeat the ruling dropped."""

import json
import types

import pytest

from backend import claude


# ─── build_history_turns ────────────────────────────────────────────────────

def _thread(*pairs):
    turns = []
    for question, answer in pairs:
        turns.append({"role": "user", "content": question})
        if answer is not None:
            turns.append({"role": "assistant", "content": answer})
    return turns


class TestBuildHistoryTurns:
    def test_alternates_and_wraps_assistant_turns_as_the_json_the_model_must_use(self):
        turns = claude.build_history_turns(_thread(
            ("Can I cook on Yom Tov?", "## Direct Answer\n\nYes, for the day itself.[1]\n\n**Sources**\n\n- Beitzah 2a — cooking"),
        ))
        assert [t["role"] for t in turns] == ["user", "assistant"]
        assert turns[0]["text"] == "Can I cook on Yom Tov?"
        reply = json.loads(turns[1]["text"])
        assert reply["ruling"].startswith("Yes, for the day itself.")
        assert "[1]" not in reply["ruling"]
        assert reply["sources"] == ["Beitzah 2a"]

    def test_an_unanswered_trailing_question_is_left_out(self):
        turns = claude.build_history_turns(_thread(("First?", "Answer."), ("Second?", None)))
        assert [t["role"] for t in turns] == ["user", "assistant"]
        assert turns[0]["text"] == "First?"

    def test_a_leading_assistant_turn_is_dropped_and_same_role_neighbours_are_joined(self):
        turns = claude.build_history_turns([
            {"role": "assistant", "content": "Orphan."},
            {"role": "user", "content": "One"},
            {"role": "user", "content": "Two"},
            {"role": "assistant", "content": "Reply"},
        ])
        assert [t["role"] for t in turns] == ["user", "assistant"]
        assert turns[0]["text"] == "One\nTwo"

    def test_empty_and_malformed_history_gives_no_turns(self):
        assert claude.build_history_turns(None) == []
        assert claude.build_history_turns([]) == []
        assert claude.build_history_turns(["x", 3, None]) == []
        assert claude.build_history_turns([{"role": "user", "content": "   "}]) == []

    def test_the_oldest_pairs_go_first_when_the_thread_is_too_long(self):
        long_answer = "Because the rule is long. " * 80
        thread = _thread(*[(f"Question {n}?", long_answer) for n in range(8)])
        turns = claude.build_history_turns(thread, assistant_chars=2400, max_total_chars=5000)
        assert turns[0]["role"] == "user" and turns[-1]["role"] == "assistant"
        assert turns[-2]["text"] == "Question 7?"
        assert sum(len(t["text"]) for t in turns) <= 5000
        assert "Question 0?" not in [t["text"] for t in turns]

    def test_a_long_turn_is_cut_at_a_sentence_end_never_mid_word(self):
        answer = "First point is made here. " * 200
        reply = json.loads(claude.build_history_turns(_thread(("Q?", answer)))[1]["text"])
        assert len(reply["ruling"]) <= claude.HISTORY_ASSISTANT_TURN_CHARS
        assert reply["ruling"].endswith("here.")


class TestClipAtBoundary:
    def test_short_text_is_untouched(self):
        assert claude._clip_at_boundary("  Short.  ", 50) == "Short."

    def test_prefers_a_sentence_end_in_the_back_half(self):
        text = "One two three four five six seven. Eight nine ten eleven twelve thirteen."
        assert claude._clip_at_boundary(text, 40) == "One two three four five six seven."

    def test_otherwise_a_word_end_with_an_ellipsis(self):
        out = claude._clip_at_boundary("alpha beta gamma delta epsilon zeta eta theta", 30)
        assert out.endswith("…")
        assert not out[:-1].endswith(" ")
        assert out[:-1] in "alpha beta gamma delta epsilon zeta eta theta"
        assert out[:-1].split()[-1] in {"alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"}


# ─── question_profile ───────────────────────────────────────────────────────

BAMIDBAR = (
    "Give me a full learning guide for Bamidbar 9-11 including the people complaining after "
    "the desert, the zekenim, Eldad and Medad, the meaning of the meat, why the ark traveled "
    "three days ahead, Chovav versus Avraham, and the clouds and fire when traveling and stopping."
)


class TestQuestionProfile:
    def test_a_learning_guide_is_a_study_question_with_the_large_ceiling(self):
        assert claude.question_profile(BAMIDBAR) == (False, True, claude.STUDY_ANSWER_MAX_TOKENS)

    @pytest.mark.parametrize("question", [
        "explain the sugya of Shabbat 31a",
        "Walk me through the laws of Pesach seder step by step",
        "Give me a source sheet on tzedakah",
        "מדריך לימוד על פרשת בהעלותך",
        "Explain Bamidbar 9-11",
    ])
    def test_other_ways_of_asking_to_learn_a_passage(self, question):
        assert claude._is_study_question(question)

    @pytest.mark.parametrize("question", [
        "can I eat this",
        "Can I turn on a light on Shabbat?",
        "What time is candle lighting?",
        "Is gelatin kosher?",
        "",
    ])
    def test_everyday_questions_are_not_study_questions(self, question):
        assert not claude._is_study_question(question)

    def test_a_short_everyday_question_keeps_the_small_ceiling(self):
        is_simple, is_study, ceiling = claude.question_profile("can I eat this")
        assert (is_simple, is_study, ceiling) == (True, False, claude.SIMPLE_ANSWER_MAX_TOKENS)

    def test_a_study_question_is_never_simple(self):
        assert claude.question_profile("teach me the parsha")[0] is False

    def test_the_prompt_for_a_study_question_asks_for_a_sectioned_full_answer(self):
        study = claude.build_prompt(BAMIDBAR, [], [])
        everyday = claude.build_prompt("Can I turn on a light on Shabbat?", [], [])
        assert study != everyday
        assert "learn" in study.lower()


# ─── truncated model output ─────────────────────────────────────────────────

class TestSalvageTruncatedOutput:
    def test_a_cut_off_ruling_is_trimmed_to_its_last_complete_sentence(self):
        raw = '{"ruling": "The people complained after leaving Sinai. Then fire consumed the edge of the camp. The mixed multitude cra'
        out = claude.parse_structured_model_output(raw)
        assert out["ruling"] == "The people complained after leaving Sinai. Then fire consumed the edge of the camp."
        assert "cra" not in out["ruling"]

    def test_trailing_headings_are_not_left_dangling(self):
        raw = '{"ruling": "First part is done.\\n\\n## Eldad and Medad\\n\\nTwo men proph'
        out = claude.parse_structured_model_output(raw)
        assert out["ruling"] == "First part is done."

    def test_finished_sources_survive_a_cut_in_the_next_one(self):
        raw = ('{"ruling": "Done.", "sources": ["Numbers 9:1 — the Pesach Sheni", '
               '"Numbers 11:16 — the elders", "Numbers 11:2')
        out = claude.parse_structured_model_output(raw)
        assert out["sources"] == ["Numbers 9:1 — the Pesach Sheni", "Numbers 11:16 — the elders"]

    def test_text_with_no_ruling_is_not_invented_into_one(self):
        assert claude._salvage_truncated_payload("no json here at all") is None
        assert claude._salvage_truncated_payload("") is None

    def test_a_complete_object_is_parsed_normally(self):
        out = claude.parse_structured_model_output(json.dumps({"ruling": "Yes.", "sources": ["A 1 — a"]}))
        assert out["ruling"] == "Yes."
        assert out["sources"] == ["A 1 — a"]

    def test_an_escaped_quote_or_newline_is_decoded(self):
        raw = '{"ruling": "He said \\"go\\".\\nThen he went. And aft'
        out = claude.parse_structured_model_output(raw)
        assert out["ruling"] == 'He said "go".\nThen he went.'


# ─── summary / steps that only repeat the ruling ────────────────────────────

class TestDropRepeatedContent:
    RULING = "Cooking on Shabbat is forbidden by the Torah, including reheating food on an open flame."

    def test_a_summary_that_restates_the_ruling_is_dropped(self):
        summary, steps = claude._drop_repeated_content(
            self.RULING, "Cooking on Shabbat is forbidden, including reheating food on a flame.", [])
        assert summary == "" and steps == []

    def test_a_summary_that_adds_something_is_kept(self):
        summary, _ = claude._drop_repeated_content(
            self.RULING, "Use a blech or a hot plate set before Shabbat if you need warm food on Friday night.", [])
        assert summary.startswith("Use a blech")

    def test_a_step_repeating_the_ruling_or_an_earlier_step_is_dropped(self):
        _, steps = claude._drop_repeated_content(self.RULING, "", [
            "Do not reheat food on an open flame on Shabbat, it is cooking.",
            "Set a covered hot plate up before candle lighting.",
            "Set up a covered hot plate before candle lighting begins.",
        ])
        assert steps == ["Set a covered hot plate up before candle lighting."]

    def test_short_lines_are_never_judged(self):
        summary, steps = claude._drop_repeated_content(self.RULING, "Ask a rabbi.", ["Ask your rabbi."])
        assert summary == "Ask a rabbi." and steps == ["Ask your rabbi."]

    def test_normalizing_a_payload_drops_the_echo(self):
        out = claude._normalize_structured_response({
            "ruling": self.RULING,
            "summary": "Cooking on Shabbat is forbidden, including reheating food on an open flame.",
            "practical_steps": ["Do not reheat food on an open flame on Shabbat."],
        })
        assert out["summary"] == "" and out["practical_steps"] == []
        markdown = claude.render_structured_markdown(out)
        assert "## Summary" not in markdown and "## What to do" not in markdown


# ─── Claude request shape: caching, history, effort ─────────────────────────

class TestAnthropicRequestPieces:
    def test_the_fixed_prompt_is_cached_and_the_per_request_context_is_not(self):
        blocks = claude._anthropic_system_blocks("CORE", "CUSTOMS FOR THIS ASK")
        assert blocks[0] == {"type": "text", "text": "CORE", "cache_control": {"type": "ephemeral"}}
        assert blocks[1] == {"type": "text", "text": "CUSTOMS FOR THIS ASK"}
        assert "cache_control" not in blocks[1]

    def test_no_dynamic_context_is_just_the_cached_block(self):
        assert claude._anthropic_system_blocks("CORE") == [
            {"type": "text", "text": "CORE", "cache_control": {"type": "ephemeral"}}]

    def test_history_becomes_messages_with_a_breakpoint_on_the_last_earlier_turn(self):
        messages = claude._anthropic_messages("new prompt", [
            {"role": "user", "text": "Q1"}, {"role": "assistant", "text": "A1"}])
        assert messages[0] == {"role": "user", "content": "Q1"}
        assert messages[1] == {"role": "assistant", "content": [
            {"type": "text", "text": "A1", "cache_control": {"type": "ephemeral"}}]}
        assert messages[2] == {"role": "user", "content": "new prompt"}

    def test_without_history_it_is_the_prompt_alone(self):
        assert claude._anthropic_messages("p") == [{"role": "user", "content": "p"}]

    def test_haiku_5_gets_an_effort_and_other_models_get_nothing(self):
        assert claude._anthropic_request_options("claude-haiku-5-5") == {"output_config": {"effort": "low"}}
        assert claude._anthropic_request_options("claude-haiku-5-5", "medium") == {"output_config": {"effort": "medium"}}
        assert claude._anthropic_request_options("claude-haiku-4-5") == {}

    def test_cache_reads_and_writes_are_billed_at_their_relative_rates(self):
        usage = types.SimpleNamespace(
            input_tokens=100, cache_creation_input_tokens=1000, cache_read_input_tokens=5000, output_tokens=300)
        input_equivalent, output = claude._anthropic_usage_tokens(usage)
        assert input_equivalent == 100 + 1250 + 500
        assert output == 300

    def test_missing_or_non_integer_usage_counts_as_zero(self):
        assert claude._anthropic_usage_tokens(None) == (0, 0)
        mock_like = types.SimpleNamespace(input_tokens=object(), output_tokens="9")
        assert claude._anthropic_usage_tokens(mock_like) == (0, 0)

    def test_the_fallback_model_is_haiku_5_5(self):
        assert claude._CLAUDE_FALLBACK_MODEL == "claude-haiku-5-5"


class _Messages:
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


def _response(text, *, stop_reason="end_turn", usage=None):
    return types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=usage or types.SimpleNamespace(
            input_tokens=10, output_tokens=20, cache_creation_input_tokens=0, cache_read_input_tokens=0),
    )


@pytest.fixture
def anthropic_calls(monkeypatch):
    """Route the fallback to a fake client; collect what it is sent and what is billed."""
    billed = []

    async def _record(**kwargs):
        billed.append(kwargs)

    monkeypatch.setattr(claude, "record_llm_call", _record)
    holder = types.SimpleNamespace(messages=None, billed=billed)

    def install(response):
        holder.messages = _Messages(response)
        monkeypatch.setattr(claude, "_get_async_client", lambda: types.SimpleNamespace(messages=holder.messages))
        return holder

    return install


@pytest.mark.asyncio
class TestAnthropicFallbackCall:
    async def test_history_effort_ceiling_and_cache_layout_reach_the_request(self, anthropic_calls):
        holder = anthropic_calls(_response(json.dumps({"ruling": "Yes."})))
        history = [{"role": "user", "text": "Q1"}, {"role": "assistant", "text": '{"ruling": "A1"}'}]

        result = await claude._call_anthropic_httpx_model(
            "now?", "CTX", history_turns=history)

        [call] = holder.messages.calls
        assert call["model"] == "claude-haiku-5-5"
        assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert call["system"][1] == {"type": "text", "text": "CTX"}
        assert [m["role"] for m in call["messages"]] == ["user", "assistant", "user"]
        assert call["output_config"] == {"effort": "low"}
        assert call["max_tokens"] == claude.COMPLEX_ANSWER_MAX_TOKENS + claude.CLAUDE_THINKING_HEADROOM_TOKENS
        # Haiku 5.5 rejects these; the request must not carry them or a beta header.
        for forbidden in ("temperature", "top_p", "top_k", "extra_headers", "betas"):
            assert forbidden not in call
        assert result["is_fallback"] is True

    async def test_a_study_ceiling_gets_the_larger_budget_and_medium_effort(self, anthropic_calls):
        holder = anthropic_calls(_response(json.dumps({"ruling": "Long."})))

        await claude._call_anthropic_httpx_model("p", max_tokens=claude.STUDY_ANSWER_MAX_TOKENS)

        [call] = holder.messages.calls
        assert call["max_tokens"] == claude.STUDY_ANSWER_MAX_TOKENS + claude.CLAUDE_THINKING_HEADROOM_TOKENS
        assert call["output_config"] == {"effort": "medium"}

    async def test_cached_tokens_are_billed_at_their_rates(self, anthropic_calls):
        usage = types.SimpleNamespace(
            input_tokens=50, cache_creation_input_tokens=0, cache_read_input_tokens=4000, output_tokens=200)
        holder = anthropic_calls(_response(json.dumps({"ruling": "Yes."}), usage=usage))

        await claude._call_anthropic_httpx_model("p")

        [entry] = holder.billed
        assert entry["provider"] == "anthropic" and entry["model"] == "claude-haiku-5-5"
        assert entry["input_tokens"] == 50 + 400
        assert entry["output_tokens"] == 200

    async def test_a_safeguard_refusal_is_an_error_result_not_an_empty_answer(self, anthropic_calls):
        anthropic_calls(types.SimpleNamespace(content=[], stop_reason="refusal", usage=None))

        result = await claude._call_anthropic_httpx_model("p", gemini_error="timeout")

        assert result["error"] == "gemini_error: timeout; anthropic_refusal"
        assert result["is_fallback"] is True

    async def test_an_answer_cut_off_by_the_limit_is_salvaged_not_shown_half_finished(self, anthropic_calls):
        cut = '{"ruling": "The elders were chosen. Seventy men received the spirit. Eldad and Med'
        anthropic_calls(_response(cut, stop_reason="max_tokens"))

        result = await claude._call_anthropic_httpx_model("p")

        assert result["structured"]["ruling"] == "The elders were chosen. Seventy men received the spirit."


# ─── Gemini contents ────────────────────────────────────────────────────────

class TestGeminiContents:
    def test_no_history_sends_the_prompt_as_is(self):
        assert claude._gemini_contents("prompt") == "prompt"
        assert claude._gemini_contents("prompt", []) == "prompt"

    def test_history_becomes_user_and_model_turns_then_the_prompt(self, monkeypatch):
        from google.genai import types as real_types

        monkeypatch.setattr(claude, "genai_types", real_types)
        contents = claude._gemini_contents("now?", [
            {"role": "user", "text": "Q1"}, {"role": "assistant", "text": "A1"}])
        assert [c.role for c in contents] == ["user", "model", "user"]
        assert [c.parts[0].text for c in contents] == ["Q1", "A1", "now?"]


# ─── the history/ceiling extras the call paths pass along ───────────────────

class TestModelCallExtras:
    def test_nothing_extra_for_a_first_everyday_question(self):
        assert claude._model_call_extras([], claude.COMPLEX_ANSWER_MAX_TOKENS) == {}
        assert claude._model_call_extras([], claude.SIMPLE_ANSWER_MAX_TOKENS) == {}

    def test_history_and_the_study_ceiling_are_passed_when_present(self):
        turns = [{"role": "user", "text": "Q"}, {"role": "assistant", "text": "A"}]
        assert claude._model_call_extras(turns, claude.STUDY_ANSWER_MAX_TOKENS) == {
            "history_turns": turns, "max_tokens": claude.STUDY_ANSWER_MAX_TOKENS}


class TestHistoryAsTurnsPrompt:
    HISTORY = [{"role": "user", "content": "Can I cook on Yom Tov?"},
               {"role": "assistant", "content": "Yes, for the day itself."}]

    def test_the_thread_is_not_pasted_into_the_prompt_when_it_is_sent_as_turns(self):
        pasted = claude.build_prompt("and Shabbat?", [], [], conversation_history=self.HISTORY)
        as_turns = claude.build_prompt(
            "and Shabbat?", [], [], conversation_history=self.HISTORY, history_as_turns=True)
        assert "Can I cook on Yom Tov?" in pasted
        assert "Can I cook on Yom Tov?" not in as_turns
        # ...but it still tells the model to resolve references against the earlier turns.
        assert "earlier turns of this conversation" in as_turns
