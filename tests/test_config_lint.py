"""
Dead-config lint (plan.md §23.4 invariant A).

`AI_MODEL_TIMEOUT_SECONDS`/`MODEL_REQUEST_TIMEOUT_SECONDS` was defined via
`_int_env(...)` for months without ever being passed to the SDK — a silently
dead env-configurable knob. This test makes that class of bug fail CI instead
of going unnoticed: every module-level constant assigned from `_int_env(...)`
must be referenced at least once outside its own definition line, somewhere
in `backend/`, `app.py`, or `asgi.py`.

Pure text scan — no imports, no env/mocking needed, so it can't be skewed by
import-time side effects.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SOURCE_FILES = [
    *sorted((REPO_ROOT / "backend").glob("*.py")),
    REPO_ROOT / "app.py",
    REPO_ROOT / "asgi.py",
]

# Matches: SOME_NAME = _int_env("ENV_VAR", default)
_INT_ENV_DEFINITION = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=\s*_int_env\(", re.MULTILINE)


def _load_sources() -> dict[Path, str]:
    return {path: path.read_text(encoding="utf-8") for path in SOURCE_FILES if path.exists()}


def _find_int_env_constants(sources: dict[Path, str]) -> dict[str, Path]:
    constants: dict[str, Path] = {}
    for path, text in sources.items():
        for match in _INT_ENV_DEFINITION.finditer(text):
            constants[match.group(1)] = path
    return constants


class TestIntEnvConstantsAreUsed:
    def test_at_least_one_constant_is_defined(self):
        # Sanity check that this test is actually exercising something —
        # if _int_env definitions ever move/rename, this should fail loudly
        # rather than silently passing on zero constants.
        sources = _load_sources()
        constants = _find_int_env_constants(sources)
        assert constants, "Expected at least one `_int_env(...)`-defined constant in backend/app.py/asgi.py"

    def test_every_int_env_constant_is_referenced_elsewhere(self):
        sources = _load_sources()
        constants = _find_int_env_constants(sources)

        dead = []
        for name, def_path in constants.items():
            name_pattern = re.compile(rf"\b{re.escape(name)}\b")
            usages = 0
            for path, text in sources.items():
                for line in text.splitlines():
                    if not name_pattern.search(line):
                        continue
                    # Skip the defining line itself (and any other
                    # `_int_env(` assignment line for this name).
                    if "_int_env(" in line and line.strip().startswith(name):
                        continue
                    usages += 1
            if usages == 0:
                dead.append(f"{name} (defined in {def_path.relative_to(REPO_ROOT)})")

        assert not dead, (
            "Found _int_env(...) constants defined but never referenced elsewhere "
            f"(dead config): {dead}"
        )


# ─── Dangling `plan.md §N` citation lint (plan.md §23.2.5 / §23.3) ───────────
#
# A prior restructure of plan.md's §7 removed its numbered subsections while
# eleven citations elsewhere in the repo (code, tests, ENGINEERING_RULES.md)
# kept pointing at them (§7.13/§7.14) -- silently orphaning the spec for
# invariants tests actively enforce. This lint is the promised "control that
# makes this the last time" (plan.md §23.3): it asserts every `plan.md §N`
# citation's *top-level* section number still exists as a heading in
# plan.md, so a future restructure that deletes/renumbers a whole section
# fails CI instead of leaving stale citations to rot unnoticed.
#
# Deliberately top-level-only, not full dotted-path validation: the large
# majority of citations (e.g. `§8.B.6`, `§20.1-C2`, `§14.4.4`) reference a
# numbered/lettered item inside a section's prose, not a separate markdown
# heading -- validating those would need a full outline parser and produce
# false positives on perfectly valid citations, not a "cheap" regex-only
# check. Top-level-only still catches the general shape of the risk (a
# whole section disappearing or being renumbered) without that cost.
PLAN_MD_PATH = REPO_ROOT / "plan.md"

_PLAN_CITATION = re.compile(r"plan\.md §(\d+)")
_PLAN_TOP_LEVEL_HEADING = re.compile(r"^##\s+(\d+)\.", re.MULTILINE)

# Broader than SOURCE_FILES above -- plan.md citations appear throughout the
# repo (tests, templates, scripts, docs, engineering rules), not just
# backend/app.py/asgi.py.
_CITATION_SCAN_GLOBS = [
    "*.py", "backend/**/*.py", "backend/utils/*.py", "tests/**/*.py",
    "scripts/**/*.py", "scripts/**/*.sql", "templates/**/*.html",
    ".agents/**/*.md", "docs/**/*.md", "plan.md", "claude_code_prompts.md",
]


def _citation_scan_files() -> list[Path]:
    seen = set()
    files = []
    for pattern in _CITATION_SCAN_GLOBS:
        for path in sorted(REPO_ROOT.glob(pattern)):
            if path.is_file() and path not in seen:
                seen.add(path)
                files.append(path)
    return files


class TestPlanMdCitationsResolve:
    def test_plan_md_has_top_level_headings(self):
        # Sanity check, same rationale as the _int_env test above: fail
        # loudly if plan.md's heading format ever changes instead of
        # silently validating against zero headings.
        text = PLAN_MD_PATH.read_text(encoding="utf-8")
        headings = set(_PLAN_TOP_LEVEL_HEADING.findall(text))
        assert headings, "Expected at least one `## N.` top-level heading in plan.md"

    def test_every_citation_top_level_section_exists(self):
        plan_text = PLAN_MD_PATH.read_text(encoding="utf-8")
        valid_sections = set(_PLAN_TOP_LEVEL_HEADING.findall(plan_text))

        dangling = []
        for path in _citation_scan_files():
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                for section in _PLAN_CITATION.findall(line):
                    if section not in valid_sections:
                        dangling.append(
                            f"{path.relative_to(REPO_ROOT)}:{lineno} cites plan.md "
                            f"§{section}, which has no `## {section}.` heading in plan.md"
                        )

        assert not dangling, (
            "Found plan.md §N citations whose top-level section no longer exists "
            "(plan.md §23.3 dangling-citation class of bug): " + "; ".join(dangling)
        )
