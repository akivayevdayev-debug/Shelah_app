"""Linear-time guards for regexes SonarCloud flags as super-linear (python:S8786).

Each pattern below used to be O(n^2) on a long run of characters it can
start matching from every position (found with an adversarial-input timing
probe). 30,000 characters takes several seconds with a quadratic pattern and
a few milliseconds with a linear one, so a one-second ceiling cannot flake on
a slow runner yet still fails loudly if the quadratic form comes back. The
behaviour assertions pin that the rewrites still match exactly what the old
patterns matched.
"""

import time

import pytest

from backend import claude
from backend import routes_library as lib

N = 30_000
CEILING_SECONDS = 1.0


def _elapsed(fn, *args):
    started = time.perf_counter()
    result = fn(*args)
    return result, time.perf_counter() - started


@pytest.mark.parametrize(
    "pattern, hostile",
    [
        (lib._DAF_TOKEN_RE, "1" * N + "x"),
        (lib._SECTION_RANGE_RE, "1" * N + "!"),
        (lib._DAF_RANGE_RE, "1" * N),
    ],
    ids=["daf_token", "section_range", "daf_range"],
)
def test_ref_patterns_stay_linear_on_long_digit_runs(pattern, hostile):
    result, seconds = _elapsed(pattern.search, hostile)
    assert result is None
    assert seconds < CEILING_SECONDS


def test_fenced_json_extraction_stays_linear_on_an_unclosed_fence_with_long_whitespace():
    result, seconds = _elapsed(claude._extract_fenced_json_object, "```" + " " * N)
    assert result is None
    assert seconds < CEILING_SECONDS


def test_ref_patterns_still_extract_the_same_pieces():
    assert lib._DAF_TOKEN_RE.search("see 12a and 3b").group() == "12a"
    assert lib._DAF_TOKEN_RE.search("no daf here 42") is None
    assert lib._SECTION_RANGE_RE.search("x 3-7 y").groups() == ("3", "7")
    assert lib._DAF_RANGE_RE.search("Berakhot 2a:1-13a:15").groups() == ("2a", "13a")
    assert lib._DAF_RANGE_RE.search("Berakhot 2A - 3B").groups() == ("2A", "3B")


def test_ref_numbers_are_bounded_to_nine_digits():
    """The digit bound is what makes the patterns linear; a longer run is not a
    citation number, so it must not match (and never as a truncated tail)."""
    assert lib._DAF_TOKEN_RE.search("123456789a").group() == "123456789a"
    assert lib._DAF_TOKEN_RE.search("1234567890a") is None
    assert lib._SECTION_RANGE_RE.search("1-123456789").groups() == ("1", "123456789")
    assert lib._SECTION_RANGE_RE.search("1-1234567890") is None
    assert lib._DAF_RANGE_RE.search("1234567890a-2b") is None
    assert lib._DAF_RANGE_RE.search("2a-1234567890b") is None


def test_fenced_json_extraction_still_reads_json_fences():
    assert claude._extract_fenced_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert claude._extract_fenced_json_object('intro ```  {"b": 2}  ``` outro') == {"b": 2}
    assert claude._extract_fenced_json_object("no fence at all") is None


def _blocked(text):
    return any(p.search(text) for p in claude.OUTPUT_POLICY_BLOCKLIST_PATTERNS)


def test_output_blocklist_stays_linear_on_long_whitespace_runs():
    for hostile in ("hidden chain" + " " * N, "hidden chain" + " -" * N, "hidden chain" + " " * N + "of"):
        blocked, seconds = _elapsed(_blocked, hostile)
        assert blocked is False
        assert seconds < CEILING_SECONDS


@pytest.mark.parametrize("text", [
    "System  Prompt", "the developer\nmessage", "INTERNAL instructions",
    "hidden chain of thought", "hidden chain-of-thought", "Hidden Chain - Of - Thought",
    "hidden chain \t of \n thought", "hidden chain\t of thought",
])
def test_output_blocklist_still_blocks_each_phrase_and_chain_of_thought_spelling(text):
    assert _blocked(text) is True


@pytest.mark.parametrize("text", [
    "hidden chainofthought",          # no separator at all
    "hidden chain--of--thought",      # two hyphens is not one hyphen
    "hidden chain\tof\tthought",      # whitespace with no plain space and no hyphen
    "systemprompt", "chain of thought",
])
def test_output_blocklist_keeps_its_previous_non_matches(text):
    assert _blocked(text) is False


def test_out_of_scope_needs_the_topic_and_no_halachic_context_on_the_first_line():
    # Context words below are deliberately not DOMAIN_MARKER_RE words (torah,
    # halachic, ... exit earlier), so it is the context pattern that decides.
    assert claude._detect_out_of_scope_subject("solve this algebra problem") == "Pure Math (no halachic context)"
    assert claude._detect_out_of_scope_subject("debug my stack  trace") == "Pure Coding (no halachic context)"
    assert claude._detect_out_of_scope_subject("debug the electricity meter") is None
    assert claude._detect_out_of_scope_subject("quantum mechanics of a vaccine") is None
    assert claude._detect_out_of_scope_subject("quantum  mechanics") == "Pure Science (no medical/halachic context)"
    assert claude._detect_out_of_scope_subject("best anime") == "Pop Culture (explicitly non-religious)"


def test_out_of_scope_only_reads_the_first_line_as_before():
    # Behaviour kept from the old `^(?!.*ctx).*\b(topic)\b`, where `.` stopped at
    # a newline: a topic on line two is not seen, and context there is not seen.
    assert claude._detect_out_of_scope_subject("hello\nalgebra") is None
    assert claude._detect_out_of_scope_subject("debug\nelectricity") == "Pure Coding (no halachic context)"


@pytest.mark.parametrize("text, expected", [
    ("```json\n{\"a\": 1}\n```", {"a": 1}),
    ("```JSON {\"a\": 1}```", {"a": 1}),
    ("```{\"a\": 1}```", {"a": 1}),
    ("x ```json{\"a\": 1}``` y ``` {\"b\": 2} ```", {"a": 1}),
    ("```json```", None),
    ("```{\"a\": 1}", None),
    ("`` `{\"a\": 1}```", None),
])
def test_fenced_block_takes_the_first_closed_fence_and_drops_a_json_tag(text, expected):
    assert claude._extract_fenced_json_object(text) == expected
