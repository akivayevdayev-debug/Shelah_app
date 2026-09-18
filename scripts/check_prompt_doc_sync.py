#!/usr/bin/env python3
"""Cross-check status claims between claude_code_prompts.md and plan.md.

Background: this project has repeatedly let claude_code_prompts.md's "Prompt
N" status badges drift out of sync with the plan.md section(s) they claim to
track (see plan.md §50/§51 for three prior instances found by hand). §51's
own recommendation was to grep plan.md for the section a prompt row claims to
track and diff the two statuses before trusting either file's claim in
isolation -- this script is that check, automated.

For every "## Prompt N -- SS: ..." entry in claude_code_prompts.md, this finds
the plan.md section(s) "SS" it references, classifies both the prompt's own
stated status and the plan.md section's stated status using a rough
emoji+keyword heuristic (done / partial / open / unknown), and reports any
pair whose classifications disagree.

This is intentionally NOT exhaustive NLP -- it is a cheap heuristic meant to
flag candidates for human review, not a source of truth. A reported mismatch
may be a false positive (ambiguous prose); an unreported one may still be
real (the heuristic missed a signal). Read both cited sections before editing
anything on the strength of this tool's output alone.

Usage:
    python3 scripts/check_prompt_doc_sync.py
    python3 scripts/check_prompt_doc_sync.py --plan plan.md --prompts claude_code_prompts.md
    python3 scripts/check_prompt_doc_sync.py --strict   # exit 1 if any mismatch found

Exit code is 0 unless --strict is passed and at least one mismatch is found.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SECTION_HEADER_RE = re.compile(r"^(#{2,3})\s+(\d+(?:\.\d+)?)\.?\s+(.*)$")
# Some prompt numbers carry a letter suffix (Prompt 4b, 29c, 33a/33b/33c) --
# plain "\d+\b" does NOT set a word boundary before a trailing letter (both
# are \w), so it silently fails to match "Prompt 33a" at all, letting that
# whole entry (and everything up to the next *purely numeric* prompt header)
# get swallowed into the body of whichever prompt precedes it. Match the
# optional letter suffix explicitly instead of relying on \b to find it.
PROMPT_HEADER_RE = re.compile(r"^##\s+Prompt\s+(\d+[a-z]?)\b")
# The section(s) a Prompt *tracks* sit between the dash and the first colon,
# e.g. "## Prompt 44 -- \xa732: findings from implementing Prompts 26/27
# (\xa713 ..., \xa714 ...)" tracks \xa732; \xa713/\xa714 after the colon are
# backstory about a *different* prompt, not this row's own tracked section,
# and must not be compared against this row's status.
PROMPT_PRIMARY_SEGMENT_RE = re.compile(r"^##\s+Prompt\s+\d+[a-z]?\s*[—–-]+\s*(.*?):")
SECTION_REF_RE = re.compile(r"\xa7(\d+(?:\.\d+)?)")
FENCE_RE = re.compile(r"```.*?```", re.DOTALL)

DONE_EMOJI = "✅"  # checkmark
PARTIAL_EMOJI = "\U0001f7e1"  # yellow circle
OPEN_EMOJI = "\U0001f534"  # red circle

# Multi-word phrases only, to keep the false-positive rate down -- bare
# "open"/"done"/"fixed" show up constantly in narrative prose that isn't a
# status claim at all (e.g. "the next open prompt", "not yet fixed").
OPEN_PHRASES = [
    r"still[ -]open",
    r"not yet (?:done|fixed|committed|resolved|actionable)",
    r"remains open",
    r"unresolved",
    r"\bnot resolved\b",
    r"\bnot fixed\b",
    r"\bunaddressed\b",
    r"genuinely still-open",
    r"reconfirmed still open",
    r"needs? an? (?:repo-owner|human|operator|interactive-session) decision",
    r"awaiting (?:a )?(?:decision|yes/no|sign-?off)",
]
DONE_PHRASES = [
    r"\bdone\b",
    r"\bresolved\b",
    r"\bclosed\b",
    r"fully green",
    r"fully resolved",
    r"\bcommitted\b",
    r"\bfixed\b",
    # bare "shipped" false-positives on "already shipped a bug/defect/regression"
    # (the bug shipped, not the fix) -- exclude that specific construction.
    r"\bshipped\b(?!\s+a\s+(?:bug|defect|regression))",
]

_NEGATION_RE = re.compile(r"\bnot\b|\bnever\b|\bnobody\b|n't|\bno\b", re.IGNORECASE)


def _search_phrases(text: str, phrases: list[str]) -> bool:
    """True if any phrase matches text and isn't negated nearby.

    Looks ~20 chars back from the match for a negation word ("not", "never",
    "isn't", ...) anywhere in that window -- catches "not done", "was never
    fixed", "hasn't been resolved", and hyphenated forms like
    "not-fully-closed", without needing a full grammar.
    """
    for pat in phrases:
        for m in re.finditer(pat, text, re.IGNORECASE):
            preceding = text[max(0, m.start() - 20) : m.start()]
            if _NEGATION_RE.search(preceding):
                continue
            return True
    return False


def _classify_paragraph(text: str) -> str:
    """Classify a single paragraph's own signals -- no cross-paragraph context."""
    has_done = DONE_EMOJI in text or _search_phrases(text, DONE_PHRASES)
    has_open = OPEN_EMOJI in text or _search_phrases(text, OPEN_PHRASES)
    has_partial = PARTIAL_EMOJI in text

    if has_partial or (has_done and has_open):
        return "partial"
    if has_done:
        return "done"
    if has_open:
        return "open"
    return "unknown"


def classify(text: str) -> str:
    """Classify a block's *current* status as 'done', 'partial', 'open', or 'unknown'.

    Fenced code blocks are stripped first -- in both files, step-by-step
    instructions live inside ``` fences and are full of narrative words
    ("done", "fixed", "open") that aren't status declarations; the actual
    status badges live in headers and the bold lead-in/trailing prose.

    Both files share a convention of *appending* a dated resolution note
    after the original problem description rather than editing it in place
    (e.g. "STEP 1 needs a human decision..." followed later by "**Done
    2026-08-23**"). Classifying the whole block at once conflates a stale
    problem description with its own later resolution. Instead, split on
    blank lines and classify each paragraph independently; the last
    paragraph that carries any signal wins, since later paragraphs are
    chronologically later annotations in this doc's own convention.

    Paragraphs carrying an actual status emoji (an intentional, deliberate
    status mark) are preferred over paragraphs whose only signal is a bare
    keyword match, which is far more likely to be a false positive (e.g.
    "record what *shipped*" describing a different, unrelated prior piece
    of work, or "closed six of its eight items" about a sibling section).
    Keyword-only paragraphs are used only when no paragraph anywhere in the
    block carries an emoji at all.
    """
    stripped = FENCE_RE.sub(" ", text)
    paragraphs = [p for p in re.split(r"\n\s*\n", stripped) if p.strip()]

    last_emoji_status = "unknown"
    last_any_status = "unknown"
    for para in paragraphs:
        status = _classify_paragraph(para)
        if status == "unknown":
            continue
        last_any_status = status
        if any(e in para for e in (DONE_EMOJI, PARTIAL_EMOJI, OPEN_EMOJI)):
            last_emoji_status = status

    return last_emoji_status if last_emoji_status != "unknown" else last_any_status


def parse_plan_sections(text: str) -> dict[str, tuple[str, str]]:
    """Map section number ("32", "32.1", ...) -> (own_text, full_text).

    ``own_text`` runs only to the very next heading of any level -- a
    section's own intro/status prose, excluding its subsections' content.
    ``full_text`` runs until the next heading at the same or shallower level,
    so a "##" section's full_text includes all of its "###" subsections.

    The distinction matters: a big umbrella section (e.g. "§32") can have
    dozens of "###" subsections in every possible status, so its full_text
    will read as a permanent 'partial' no matter what. A Prompt row that
    cites the bare parent number should be compared against what the parent
    section's *own* header/intro claims, not against the noisy union of
    every child subsection -- full_text is kept only as a fallback for when
    the section has no subsection structure and own_text carries no signal.
    """
    lines = text.splitlines()
    headers: list[tuple[int, int, str]] = []
    for i, line in enumerate(lines):
        m = SECTION_HEADER_RE.match(line)
        if m:
            headers.append((i, len(m.group(1)), m.group(2)))

    sections: dict[str, tuple[str, str]] = {}
    for idx, (start, level, num) in enumerate(headers):
        own_end = headers[idx + 1][0] if idx + 1 < len(headers) else len(lines)
        full_end = len(lines)
        for j in range(idx + 1, len(headers)):
            if headers[j][1] <= level:
                full_end = headers[j][0]
                break
        own_text = "\n".join(lines[start:own_end])
        full_text = "\n".join(lines[start:full_end])
        sections[num] = (own_text, full_text)
    return sections


APPENDIX_START_RE = re.compile(r"^Revised for the .* status snapshot", re.IGNORECASE)


def parse_prompts(text: str) -> dict[str, dict[str, str]]:
    """Map prompt number -> {"header": ..., "body": ...}."""
    lines = text.splitlines()
    headers: list[tuple[int, str]] = []
    appendix_start = len(lines)
    for i, line in enumerate(lines):
        m = PROMPT_HEADER_RE.match(line)
        if m:
            headers.append((i, m.group(1)))
        elif APPENDIX_START_RE.match(line):
            # The last numbered prompt is followed, with no heading of its
            # own, by a "Suggested execution order" appendix (a long table
            # covering every prompt/section with its own status wording).
            # Without this cutoff the last prompt's body would swallow that
            # whole appendix and get classified from unrelated rows.
            appendix_start = min(appendix_start, i)

    prompts: dict[str, dict[str, str]] = {}
    for idx, (start, num) in enumerate(headers):
        end = headers[idx + 1][0] if idx + 1 < len(headers) else len(lines)
        end = min(end, appendix_start) if end > appendix_start > start else end
        body = "\n".join(lines[start:end])
        prompts[num] = {"header": lines[start], "body": body}
    return prompts


def find_mismatches(
    plan_sections: dict[str, tuple[str, str]], prompts: dict[str, dict[str, str]]
) -> list[dict]:
    findings = []
    def _sort_key(kv: tuple[str, dict]) -> tuple[int, str]:
        m = re.match(r"(\d+)([a-z]?)", kv[0])
        return (int(m.group(1)), m.group(2)) if m else (0, kv[0])

    for pnum, prompt in sorted(prompts.items(), key=_sort_key):
        segment_m = PROMPT_PRIMARY_SEGMENT_RE.match(prompt["header"])
        segment = segment_m.group(1) if segment_m else prompt["header"]
        refs = list(dict.fromkeys(SECTION_REF_RE.findall(segment)))
        if not refs:
            continue
        prompt_status = classify(prompt["body"])
        if prompt_status == "unknown":
            continue
        for ref in refs:
            texts = plan_sections.get(ref)
            if texts is None:
                continue
            own_text, full_text = texts
            section_status = classify(own_text)
            if section_status == "unknown":
                section_status = classify(full_text)
            if section_status == "unknown":
                continue
            if prompt_status != section_status:
                findings.append(
                    {
                        "prompt": pnum,
                        "section": ref,
                        "prompt_status": prompt_status,
                        "section_status": section_status,
                    }
                )
    return findings


def resolve_repo_path(value: str, repo_root: Path | None = None) -> Path:
    """Resolve a CLI-supplied path and require it stays inside the repo root.

    Relative paths resolve against the current directory, as they always did
    (so ``--plan ../plan.md`` from ``scripts/`` still works); symlinks and
    ``..`` segments are collapsed with ``os.path.realpath`` *before* the
    containment check, so neither ``../`` traversal nor a symlink pointing out
    of the repo can escape it. Raises ValueError for anything that resolves
    outside the root (SonarCloud pythonsecurity:S8707).
    """
    root = os.path.realpath(REPO_ROOT if repo_root is None else repo_root)
    resolved = os.path.realpath(value)
    if os.path.commonpath([root, resolved]) != root:
        raise ValueError(f"{value!r} resolves outside the repository root ({root})")
    return Path(resolved)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", default="plan.md")
    parser.add_argument("--prompts", default="claude_code_prompts.md")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 if any mismatch is found (default: always exit 0, advisory only)",
    )
    args = parser.parse_args(argv)

    try:
        plan_path = resolve_repo_path(args.plan)
        prompts_path = resolve_repo_path(args.prompts)
    except ValueError as exc:
        parser.error(str(exc))  # prints usage + message to stderr, exits 2
    if not plan_path.exists() or not prompts_path.exists():
        print(f"check_prompt_doc_sync: skipping (missing {plan_path} or {prompts_path})")
        return 0

    plan_sections = parse_plan_sections(plan_path.read_text())
    prompts = parse_prompts(prompts_path.read_text())
    findings = find_mismatches(plan_sections, prompts)

    if not findings:
        print("check_prompt_doc_sync: no status mismatches found (advisory heuristic).")
        return 0

    print(
        f"check_prompt_doc_sync: {len(findings)} candidate mismatch(es) -- "
        "REVIEW, don't auto-trust:\n"
    )
    for f in findings:
        print(
            f"  Prompt {f['prompt']} claims '{f['prompt_status']}' "
            f"but plan.md \xa7{f['section']} reads '{f['section_status']}'"
        )
    print(
        "\nThis is a rough heuristic (see the script's own docstring) -- read both "
        "sides before editing either file."
    )
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
