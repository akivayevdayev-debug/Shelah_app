"""
Dead-config lint (plan.md §7.14 invariant #2).

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
