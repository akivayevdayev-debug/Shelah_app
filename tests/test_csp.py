"""
script-src has no 'unsafe-inline'; the page's own inline scripts are admitted
by SHA-256 hash (backend/csp.py).

What can go wrong is not visible from the header string: a hash that does not
match what the browser computes blocks a bootstrap script with no server-side
error (CSP blocks are invisible to curl). So the rendered pages are checked
against an independent oracle -- the standard library's HTML parser, not the
regex the production code uses -- for every page that carries inline scripts.
"""

from __future__ import annotations

import base64
import hashlib
from html.parser import HTMLParser

import pytest

from backend import csp
from backend.helpers import SECURITY_RESPONSE_HEADERS

HTML_ROUTES = [
    "/",
    "/about",
    "/terms",
    "/privacy",
    "/help",
    "/glossary",
    "/licenses",
    "/dmca",
    "/accessibility",
    "/acceptable-use",
    "/ai-disclosure",
    "/zz-not-a-page",  # the 404 template
]

# <script type=...> values the browser never executes.
DATA_TYPES = {"application/json", "application/ld+json"}


class _InlineScripts(HTMLParser):
    """Collects (type, text) of every <script> without src, and every tag
    attribute whose name starts with "on" (an inline event handler)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.scripts: list[tuple[str, str]] = []
        self.handlers: list[tuple[str, str]] = []
        self._current: list | None = None

    def handle_starttag(self, tag, attrs):
        attr_map = {name: (value or "") for name, value in attrs}
        for name in attr_map:
            if name.startswith("on"):
                self.handlers.append((tag, name))
        if tag == "script" and "src" not in attr_map:
            self._current = [attr_map.get("type", "").strip().lower(), []]

    def handle_data(self, data):
        if self._current is not None:
            self._current[1].append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._current is not None:
            kind, parts = self._current
            self.scripts.append((kind, "".join(parts)))
            self._current = None


def _sha256_b64(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii")


def _script_src(policy: str) -> str:
    return next(part.strip() for part in policy.split(";") if part.strip().startswith("script-src "))


class TestInlineScriptHashes:
    def test_hashes_an_inline_script_the_way_the_browser_does(self):
        body = "window.a = 1;"
        assert csp.inline_script_hashes(f"<script>{body}</script>") == [_sha256_b64(body)]

    def test_skips_external_scripts_including_ones_with_data_src_lookalikes(self):
        html = '<script src="/a.js"></script><script data-src="x">var b;</script>'
        assert csp.inline_script_hashes(html) == [_sha256_b64("var b;")]

    def test_skips_data_blocks_but_not_modules_or_import_maps(self):
        html = (
            '<script type="application/ld+json">{"a":1}</script>'
            '<script type="application/json" id="x">{}</script>'
            '<script type="module">import "/a.js";</script>'
            "<script type='importmap'>{\"imports\":{}}</script>"
        )
        assert csp.inline_script_hashes(html) == [
            _sha256_b64('import "/a.js";'),
            _sha256_b64('{"imports":{}}'),
        ]

    def test_normalises_line_endings_like_the_html_parser(self):
        assert csp.inline_script_hashes("<script>a\r\nb\rc</script>") == [_sha256_b64("a\nb\nc")]

    def test_deduplicates_and_ignores_empty_scripts(self):
        html = "<script>x()</script><script></script><script>x()</script>"
        assert csp.inline_script_hashes(html) == [_sha256_b64("x()")]

    def test_script_text_containing_a_lone_open_tag_is_one_script(self):
        body = "var s = '<script>';"
        assert csp.inline_script_hashes(f"<script>{body}</script>") == [_sha256_b64(body)]


class TestPolicy:
    def test_script_src_has_no_unsafe_inline_and_attributes_are_refused(self):
        policy = csp.build_content_security_policy(["abc="])
        script_src = _script_src(policy)
        assert "'unsafe-inline'" not in script_src
        assert "'sha256-abc='" in script_src
        assert "script-src-attr 'none'" in policy

    def test_script_src_directive_comes_first_for_tests_that_look_for_it(self):
        policy = csp.build_content_security_policy()
        parts = [part.strip() for part in policy.split(";")]
        first = next(part for part in parts if "script-src" in part)
        assert first.startswith("script-src ")

    def test_style_src_still_allows_inline_styles(self):
        # The second half of the refactor, documented as open in docs/SECURITY.md.
        policy = csp.build_content_security_policy()
        style_src = next(p.strip() for p in policy.split(";") if p.strip().startswith("style-src "))
        assert "'unsafe-inline'" in style_src

    def test_the_default_policy_admits_no_inline_script(self):
        assert SECURITY_RESPONSE_HEADERS["Content-Security-Policy"] == csp.DEFAULT_CONTENT_SECURITY_POLICY
        assert "sha256-" not in csp.DEFAULT_CONTENT_SECURITY_POLICY
        assert "'unsafe-inline'" not in _script_src(csp.DEFAULT_CONTENT_SECURITY_POLICY)


class TestRenderedPages:
    @pytest.mark.parametrize("route", HTML_ROUTES)
    def test_policy_admits_exactly_the_inline_scripts_the_page_ships(self, test_client, route):
        response = test_client.get(route)
        policy = response.headers["Content-Security-Policy"]
        script_src = _script_src(policy)

        parser = _InlineScripts()
        parser.feed(response.get_data(as_text=True))
        parser.close()
        executable = {
            _sha256_b64(text)
            for kind, text in parser.scripts
            if text and kind not in DATA_TYPES
        }

        assert executable, f"{route} has no inline scripts; the test is not testing anything"
        admitted = {
            token[len("'sha256-"):-1]
            for token in script_src.split()
            if token.startswith("'sha256-")
        }
        assert admitted == executable
        assert "'unsafe-inline'" not in script_src

    @pytest.mark.parametrize("route", HTML_ROUTES)
    def test_no_page_ships_an_inline_event_handler(self, test_client, route):
        parser = _InlineScripts()
        parser.feed(test_client.get(route).get_data(as_text=True))
        parser.close()
        assert parser.handlers == []

    def test_a_json_response_gets_the_default_policy(self, test_client):
        response = test_client.get("/api/health")
        assert response.headers["Content-Security-Policy"] == csp.DEFAULT_CONTENT_SECURITY_POLICY

    def test_a_file_passthrough_is_not_read_for_hashing(self, test_client):
        response = test_client.get("/static/offline.html")
        assert response.headers["Content-Security-Policy"] == csp.DEFAULT_CONTENT_SECURITY_POLICY

    async def test_native_fastapi_route_gets_the_strict_default(self, fastapi_client):
        response = await fastapi_client.post("/ask", json={"question": "What is Shabbat?"})
        policy = response.headers["Content-Security-Policy"]
        assert policy == csp.DEFAULT_CONTENT_SECURITY_POLICY
        assert "'unsafe-inline'" not in _script_src(policy)
