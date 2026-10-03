"""
CSS keyframes guard.

Every `@keyframes` rule lives in static/css/tokens.css (the motion token
layer); feature sheets and inline template styles apply them by name. A
keyframe defined anywhere else has no shared definition to keep in step with
the reduced-motion handling and the PR gate in .agents/ENGINEERING_RULES.md
("zero new @keyframes outside tokens.css").

Pure text scan with comments stripped, so prose that mentions the rule does
not trip it. The generated Tailwind bundle is skipped; it is build output.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKENS = REPO_ROOT / "static" / "css" / "tokens.css"

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_KEYFRAMES = re.compile(r"@(?:-webkit-)?keyframes\s+([\w-]+)")


def _stylesheets() -> list[Path]:
    css = sorted((REPO_ROOT / "static" / "css").glob("*.css"))
    css.append(REPO_ROOT / "static" / "style.css")
    templates = sorted((REPO_ROOT / "templates").rglob("*.html"))
    skipped = {TOKENS, REPO_ROOT / "static" / "css" / "tailwind.css"}
    return [p for p in css + templates if p.exists() and p not in skipped]


def _strip_comments(text: str) -> str:
    return _HTML_COMMENT.sub("", _BLOCK_COMMENT.sub("", text))


def test_keyframes_are_defined_only_in_tokens_css() -> None:
    offenders: dict[str, list[str]] = {}
    for path in _stylesheets():
        names = _KEYFRAMES.findall(_strip_comments(path.read_text(encoding="utf-8")))
        if names:
            offenders[str(path.relative_to(REPO_ROOT))] = names
    assert not offenders, (
        "@keyframes belongs in static/css/tokens.css; found in: "
        + ", ".join(f"{p} ({', '.join(n)})" for p, n in offenders.items())
    )


def test_every_animation_name_used_by_the_ask_panel_is_defined_in_tokens_css() -> None:
    tokens = _strip_comments(TOKENS.read_text(encoding="utf-8"))
    defined = set(_KEYFRAMES.findall(tokens))
    panel = _strip_comments((REPO_ROOT / "static" / "css" / "conversation.css").read_text(encoding="utf-8"))
    used = set(re.findall(r"animation:\s*(conv-[\w-]+)", panel))
    assert used, "conversation.css no longer uses any conv-* animation; drop this test"
    assert used <= defined, f"conversation.css animates undefined keyframes: {sorted(used - defined)}"
