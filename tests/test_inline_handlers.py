"""
No inline event handlers: markup uses data-onclick / data-oninput, dispatched
by static/js/actions.js (see backend/csp.py for why).

The Content-Security-Policy refuses an inline handler at runtime, silently, in
the browser only. These tests make the same mistake fail the build instead.
They read the source files, so they also cover HTML that JavaScript builds in
template strings -- which the rendered-page checks in test_csp.py cannot see.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

ACTIONS_JS = (REPO / "static/js/actions.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "templates/index.html").read_text(encoding="utf-8")


def _tracked(*prefixes: str, suffixes: tuple[str, ...]) -> list[Path]:
    names = subprocess.check_output(["git", "ls-files", *prefixes], cwd=REPO, text=True).split("\n")
    return [REPO / n for n in names if n.endswith(suffixes)]


TEMPLATES = _tracked("templates", suffixes=(".html",))
APP_JS = [p for p in _tracked("static/js", "templates", suffixes=(".js", ".html"))]

# <tag ... onclick="..." / onclick='...': an attribute, not prose that mentions one.
INLINE_HANDLER = re.compile(r"""<[a-zA-Z][^<>]*?\son[a-z]+\s*=\s*["']""", re.IGNORECASE)
JS_HANDLER_ATTR = re.compile(r"""setAttribute\(\s*["']on[a-z]+["']""", re.IGNORECASE)


def _declared(name: str) -> list[str]:
    """ACTIONS or ATTRIBUTES, read out of actions.js."""
    block = re.search(rf"const {name} = Object\.freeze\(\[(.*?)\]\)", ACTIONS_JS, re.S)
    assert block, f"{name} not found in actions.js"
    return re.findall(r"'([^']+)'", block.group(1))


ACTIONS = _declared("ACTIONS")
ATTRIBUTES = _declared("ATTRIBUTES")


def test_no_template_or_script_builds_an_inline_event_handler():
    offenders = []
    for path in APP_JS:
        text = path.read_text(encoding="utf-8")
        for match in INLINE_HANDLER.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(REPO)}:{line}: {match.group(0)[-60:]}")
        for match in JS_HANDLER_ATTR.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(REPO)}:{line}: {match.group(0)}")
    assert offenders == [], "inline handlers are blocked by the CSP; use data-onclick:\n" + "\n".join(offenders)


def test_no_template_links_to_a_javascript_url():
    offenders = [
        str(path.relative_to(REPO))
        for path in TEMPLATES
        if re.search(r"""(?:href|src|action|formaction)\s*=\s*["']\s*javascript:""", path.read_text(encoding="utf-8"), re.I)
    ]
    assert offenders == []


def test_every_data_onclick_in_the_templates_names_an_allowlisted_action():
    used: set[str] = set()
    for path in APP_JS:
        text = path.read_text(encoding="utf-8")
        used.update(re.findall(r"""data-on(?:click|input)\s*=\s*["']([^"']+)["']""", text))
    assert used, "found no data-onclick at all; the pattern is wrong"
    assert used - set(ACTIONS) == set(), "not in static/js/actions.js ACTIONS, so the dispatcher ignores them"


def test_every_allowlisted_action_is_still_in_use_and_defined():
    used: set[str] = set()
    defined = ""
    for path in APP_JS:
        text = path.read_text(encoding="utf-8")
        used.update(re.findall(r"""data-on(?:click|input)\s*=\s*["']([^"']+)["']""", text))
        defined += text
    assert set(ACTIONS) - used == set(), "in ACTIONS but no markup uses it: remove it (the list is a security boundary)"
    for name in ACTIONS:
        assert re.search(rf"function\s+{name}\b|window\.{name}\s*=", defined), f"{name} is allowlisted but defined nowhere"


def test_actions_list_is_sorted_and_unique():
    assert ACTIONS == sorted(set(ACTIONS))


def test_answer_html_cannot_carry_the_action_attributes():
    # DOMPurify keeps data-* by default; the literal in index.html is read
    # before actions.js loads, so it duplicates ATTRIBUTES and must match.
    literal = re.search(r"const ACTION_ATTRIBUTES = \[(.*?)\];", INDEX_HTML, re.S)
    assert literal, "ACTION_ATTRIBUTES is gone from index.html"
    assert re.findall(r"'([^']+)'", literal.group(1)) == ATTRIBUTES
    assert "FORBID_ATTR: ACTION_ATTRIBUTES" in INDEX_HTML


def test_the_dispatcher_loads_after_the_script_that_records_the_clicked_control():
    # Capture listeners on `document` run in registration order. The inline
    # script's listener records the clicked control (markTriggerPending reads
    # it), and an inline onclick always ran after every capture listener.
    tracker = INDEX_HTML.index("_lastClickedControl = event.target")
    dispatcher = INDEX_HTML.index('<script src="/static/js/actions.js')
    assert dispatcher > tracker


def test_every_page_with_a_dispatcher_attribute_loads_the_dispatcher():
    for path in TEMPLATES:
        text = path.read_text(encoding="utf-8")
        if re.search(r"""\sdata-on(?:click|input)\s*=""", text) and path.name not in ("legal_topbar.html",):
            assert "/static/js/actions.js" in text, f"{path.name} uses data-onclick but never loads actions.js"
    # legal_topbar.html is included by pages that include legal_scripts.html.
    assert "/static/js/actions.js" in (REPO / "templates/components/legal_scripts.html").read_text(encoding="utf-8")
