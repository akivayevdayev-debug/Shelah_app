"""
Tests for backend/routes_spa_paths.py (deep-link Phase 5).

Every path static/js/router.js writes must serve the SPA shell -- the same
page as ``/`` -- on a cold open or refresh, through the ASGI stack as well as
Flask directly. The routes must not swallow /api/*, /static/* or the real
multi-page HTML (/about, legal pages), and vercel.json must stay free of the
catch-all rewrite that once 404'd production.
"""

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

SHELL_PATHS = [
    "/text/Genesis.1",
    "/text/Shulchan%20Arukh,%20Orach%20Chayim%20345:1",
    "/text/a%2Fb",
    "/text/Genesis.1/",
    "/prayer/shacharit",
    "/prayer/birkat%20hamazon",
    "/calendar/2026-09-25",
    "/answer/0b6a3f58-2f5e-4c1d-9a7e-3d2b1c0a9f88",
    "/a/Zx9_-abcDEF0123456789q",
    "/history",
    "/history/",
]

PRIVATE_PATHS = [
    "/answer/0b6a3f58-2f5e-4c1d-9a7e-3d2b1c0a9f88",
    "/a/Zx9_-abcDEF0123456789q",
    "/history",
]


def _shell_marker(html):
    # The SPA shell's own markers: the router's module entry and the AI modal.
    return 'src="/static/js/main.js' in html and 'id="aiAssistantModal"' in html


class TestShellPaths:
    @pytest.mark.parametrize("path", SHELL_PATHS)
    def test_serves_the_same_shell_as_home(self, test_client, path):
        home = test_client.get("/")
        response = test_client.get(path)
        assert response.status_code == 200, path
        assert "text/html" in response.content_type
        html = response.get_data(as_text=True)
        assert _shell_marker(html)
        assert html == home.get_data(as_text=True)

    @pytest.mark.parametrize("path", PRIVATE_PATHS)
    def test_answer_and_history_paths_are_noindex(self, test_client, path):
        response = test_client.get(path)
        assert response.headers["X-Robots-Tag"] == "noindex, nofollow"

    @pytest.mark.parametrize("path", ["/text/Genesis.1", "/prayer/shacharit", "/calendar/2026-09-25"])
    def test_library_paths_stay_indexable(self, test_client, path):
        assert "X-Robots-Tag" not in test_client.get(path).headers

    @pytest.mark.parametrize("query", [
        "chat=0b6a3f58-2f5e-4c1d-9a7e-3d2b1c0a9f88", "a=Zx9_-abcDEF0123456789q", "history=1", "text=Genesis.1&chat=x",
    ])
    def test_legacy_query_links_to_answers_are_noindex_too(self, test_client, query):
        assert test_client.get(f"/?{query}").headers["X-Robots-Tag"] == "noindex, nofollow"

    @pytest.mark.parametrize("url", ["/", "/?text=Genesis.1", "/?chat=", "/about?chat=x"])
    def test_other_pages_and_queries_stay_indexable(self, test_client, url):
        assert "X-Robots-Tag" not in test_client.get(url).headers

    @pytest.mark.parametrize("path", ["/text", "/text/", "/prayer/", "/answer/", "/a/", "/calendar/", "/history/extra"])
    def test_a_path_without_its_value_is_not_captured(self, test_client, path):
        assert test_client.get(path).status_code == 404

    @pytest.mark.parametrize("path", ["/text/Genesis.1", "/history"])
    def test_only_get_is_routed(self, test_client, path):
        assert test_client.post(path).status_code == 405

    async def test_paths_reach_the_shell_through_asgi(self, fastapi_client):
        for path in SHELL_PATHS:
            response = await fastapi_client.get(path)
            assert response.status_code == 200, path
            assert _shell_marker(response.text), path


class TestRealRoutesAreNotCaptured:
    @pytest.mark.parametrize("path", ["/about", "/help", "/terms", "/privacy", "/ai-disclosure"])
    def test_multi_page_html_is_still_its_own_page(self, test_client, path):
        response = test_client.get(path)
        assert response.status_code == 200
        assert not _shell_marker(response.get_data(as_text=True)), path

    def test_api_paths_keep_their_json_404(self, test_client):
        response = test_client.get("/api/no-such-route/text/Genesis.1")
        assert response.status_code == 404
        assert not _shell_marker(response.get_data(as_text=True))

    def test_public_answer_api_is_not_the_shell(self, test_client):
        response = test_client.get("/api/public/answer/short")
        assert response.status_code == 404
        assert response.is_json

    def test_static_files_are_still_static(self, test_client):
        response = test_client.get("/static/js/router.js")
        assert response.status_code == 200
        assert "javascript" in response.content_type
        response.close()

    @pytest.mark.parametrize("path", ["/robots.txt", "/sitemap.xml", "/manifest.webmanifest", "/service-worker.js"])
    def test_root_files_are_untouched(self, test_client, path):
        response = test_client.get(path)
        assert response.status_code == 200
        assert not _shell_marker(response.get_data(as_text=True))
        response.close()


def test_vercel_json_has_no_catch_all_rewrite():
    """Vercel's implicit routing already sends every path to the ASGI app
    with the path intact; a rewrite/route block here collapsed every request
    to ``/api/index`` and 404'd production (commits 1015e03, ef33165)."""
    config = json.loads((REPO_ROOT / "vercel.json").read_text(encoding="utf-8"))
    assert "rewrites" not in config
    assert "routes" not in config
