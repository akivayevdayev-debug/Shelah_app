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
