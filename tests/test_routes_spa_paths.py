"""
Tests for backend/routes_spa_paths.py (deep-link Phase 5).

Every path static/js/router.js writes must serve the SPA shell -- the same
page as ``/`` -- on a cold open or refresh, through the ASGI stack as well as
Flask directly. The routes must not swallow /api/*, /static/* or the real
multi-page HTML (/about, legal pages), and vercel.json must stay free of the
catch-all rewrite that once 404'd production.
"""

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

SHELL_PATHS = [
    "/text/Genesis.1",
    "/text/Shulchan%20Arukh,%20Orach%20Chayim%20345:1",
    "/text/a%2Fb",
    "/text/Genesis+1",
    "/prayer/shacharit",
    "/prayer/birkat%20hamazon",
    "/community/Ashkenaz",
    "/community/Greek-Romaniote",
    "/calendar/2026-09-25",
    "/answer/0b6a3f58-2f5e-4c1d-9a7e-3d2b1c0a9f88",
    "/a/Zx9_-abcDEF0123456789q",
    "/history",
    # Formal URLs: the overlay and AI tail after a view (router.js "Paths").
    "/chat/new",
    "/chat/0b6a3f58-2f5e-4c1d-9a7e-3d2b1c0a9f88/all/balanced/full",
    "/answer/0b6a3f58-2f5e-4c1d-9a7e-3d2b1c0a9f88/sefardic/strict",
    "/a/Zx9_-abcDEF0123456789q/greek-romaniote",
    "/text/Genesis.1/chat/new/ashkenaz/sources/mini",
    "/text/Genesis.1/calendar/2026-09-25",
    "/prayer/shacharit/signin",
    "/community/Ashkenaz/chat/c1",
    "/calendar/2026-09-25/chat/c1/full",
    "/history/chat/c1",
    "/signin",
    "/profile",
    "/settings",
]

PRIVATE_PATHS = [
    "/answer/0b6a3f58-2f5e-4c1d-9a7e-3d2b1c0a9f88",
    "/a/Zx9_-abcDEF0123456789q",
    "/history",
    "/chat/new/all/balanced",
    "/answer/0b6a3f58-2f5e-4c1d-9a7e-3d2b1c0a9f88/sefardic/strict",
    "/history/chat/c1",
    "/signin",
]


def _body(html):
    return html.split("</head>", 1)[1]


def _head_values(html):
    head = html.split("</head>", 1)[0]
    title = re.search(r"<title>(.*?)</title>", head).group(1)
    canonicals = re.findall(r'<link rel="canonical" href="([^"]*)">', head)
    og_url = re.search(r'<meta property="og:url" content="([^"]*)">', head).group(1)
    og_title = re.search(r'<meta property="og:title" content="([^"]*)">', head).group(1)
    return {"title": title, "canonicals": canonicals, "og_url": og_url, "og_title": og_title}


def _shell_marker(html):
    # The SPA shell's own markers: the router's module entry and the AI panel.
    return 'src="/static/js/main.js' in html and 'id="convPanel"' in html


class TestShellPaths:
    @pytest.mark.parametrize("path", SHELL_PATHS)
    def test_serves_the_same_shell_as_home(self, test_client, path):
        home = test_client.get("/")
        response = test_client.get(path)
        assert response.status_code == 200, path
        assert "text/html" in response.content_type
        html = response.get_data(as_text=True)
        assert _shell_marker(html)
        # Only the <head> is per-URL (TestPageMeta below).
        assert _body(html) == _body(home.get_data(as_text=True))

    @pytest.mark.parametrize("path", PRIVATE_PATHS)
    def test_answer_and_history_paths_are_noindex(self, test_client, path):
        response = test_client.get(path)
        assert response.headers["X-Robots-Tag"] == "noindex, nofollow"

    @pytest.mark.parametrize("path", ["/text/Genesis.1", "/prayer/shacharit", "/community/Ashkenaz", "/calendar/2026-09-25"])
    def test_library_paths_stay_indexable(self, test_client, path):
        assert "X-Robots-Tag" not in test_client.get(path).headers

    @pytest.mark.parametrize("query", [
        "chat=0b6a3f58-2f5e-4c1d-9a7e-3d2b1c0a9f88", "a=Zx9_-abcDEF0123456789q", "history=1", "text=Genesis.1&chat=x",
        "conversation=0b6a3f58-2f5e-4c1d-9a7e-3d2b1c0a9f88", "conversation=abc&cv=overlay",
    ])
    def test_legacy_query_links_to_answers_are_noindex_too(self, test_client, query):
        assert test_client.get(f"/?{query}").headers["X-Robots-Tag"] == "noindex, nofollow"

    @pytest.mark.parametrize("url", ["/", "/?text=Genesis.1", "/?chat=", "/about?chat=x"])
    def test_other_pages_and_queries_stay_indexable(self, test_client, url):
        assert "X-Robots-Tag" not in test_client.get(url).headers

    @pytest.mark.parametrize("path", ["/text", "/text/", "/prayer/", "/community/", "/answer/", "/a/", "/calendar/", "/history/extra"])
    def test_a_path_without_its_value_is_not_captured(self, test_client, path):
        assert test_client.get(path).status_code == 404

    @pytest.mark.parametrize("path", [
        "/chat", "/chat/", "/chat/c1/extra.words", "/chat/c1/sefardic/ashkenaz", "/chat/c1/full/mini",
        "/answer/h1/full", "/answer/h1/calendar/soon", "/calendar/2026-09-25/strict", "/history/sefardic",
        "/signin/extra", "/sefardic", "/strict",
    ])
    def test_a_tail_the_router_cannot_read_is_a_404(self, test_client, path):
        assert test_client.get(path).status_code == 404, path

    def test_a_tail_leaves_the_view_as_the_canonical_page(self, test_client):
        head = _head_values(test_client.get("/text/Genesis.1/chat/new/all/balanced/mini").get_data(as_text=True))
        assert head["canonicals"] == [SITE + "/text/Genesis.1"]
        assert head["title"] == "Genesis 1 · Sh&#39;elah"
        cal = _head_values(test_client.get("/calendar/2026-09-25/chat/c1").get_data(as_text=True))
        assert cal["canonicals"] == [SITE + "/calendar/2026-09-25"]

    @pytest.mark.parametrize("path", ["/text/Genesis.1", "/history"])
    def test_only_get_is_routed(self, test_client, path):
        assert test_client.post(path).status_code == 405

    @pytest.mark.parametrize("path, location", [
        ("/text/Genesis.1/", "/text/Genesis.1"),
        ("/text/Genesis.1//", "/text/Genesis.1"),
        ("/text/Shulchan_Arukh,_Orach_Chayim.345.1/", "/text/Shulchan_Arukh,_Orach_Chayim.345.1"),
        ("/text/%D7%91%D7%A8%D7%90%D7%A9%D7%99%D7%AA.1/", "/text/%D7%91%D7%A8%D7%90%D7%A9%D7%99%D7%AA.1"),
        ("/prayer/shacharit/", "/prayer/shacharit"),
        ("/community/Ashkenaz/", "/community/Ashkenaz"),
        ("/calendar/2026-09-25/", "/calendar/2026-09-25"),
        ("/answer/0b6a3f58-2f5e-4c1d-9a7e-3d2b1c0a9f88/", "/answer/0b6a3f58-2f5e-4c1d-9a7e-3d2b1c0a9f88"),
        ("/a/Zx9_-abcDEF0123456789q/", "/a/Zx9_-abcDEF0123456789q"),
        ("/history/", "/history"),
        ("/chat/new/all/balanced/", "/chat/new/all/balanced"),
        ("/text/Genesis.1/?layout=parallel&conversation=abc", "/text/Genesis.1?layout=parallel&conversation=abc"),
    ])
    def test_a_trailing_slash_redirects_to_the_bare_path(self, test_client, path, location):
        response = test_client.get(path)
        assert response.status_code == 308, path
        assert response.headers["Location"] == location
        # Following it lands on the shell.
        assert test_client.get(location).status_code == 200

    def test_head_redirects_too_and_post_is_left_alone(self, test_client):
        assert test_client.head("/text/Genesis.1/").status_code == 308
        assert test_client.post("/text/Genesis.1/").status_code != 308

    @pytest.mark.parametrize("path", ["/about/", "/api/", "/static/", "/texts/Genesis/"])
    def test_other_slashed_paths_are_not_redirected(self, test_client, path):
        assert test_client.get(path).status_code != 308

    async def test_trailing_slash_redirect_through_asgi(self, fastapi_client):
        response = await fastapi_client.get("/text/Genesis.1/?layout=parallel")
        assert response.status_code == 308
        assert response.headers["location"] == "/text/Genesis.1?layout=parallel"

    async def test_paths_reach_the_shell_through_asgi(self, fastapi_client):
        for path in SHELL_PATHS:
            response = await fastapi_client.get(path)
            assert response.status_code == 200, path
            assert _shell_marker(response.text), path


class TestMissingText:
    """/text/<ref> for a text the reader has already found doesn't exist
    (sefaria_library.is_known_missing_text, never a Sefaria request) is a
    real 404 with the shell, kept out of search results (audit U-14)."""

    @pytest.fixture(autouse=True)
    def _blorp_is_missing(self, monkeypatch):
        from backend import sefaria_library
        asked = []

        def known_missing(ref):
            asked.append(ref)
            return ref == "Blorp 4"

        monkeypatch.setattr(sefaria_library, "is_known_missing_text", known_missing)
        return asked

    @pytest.mark.parametrize("path", ["/text/Blorp.4", "/text/Blorp%204", "/text/Blorp+4"])
    def test_a_known_missing_text_is_a_404_shell(self, test_client, path):
        response = test_client.get(path)
        html = response.get_data(as_text=True)
        assert response.status_code == 404
        assert response.headers["X-Robots-Tag"] == "noindex, nofollow"
        assert _shell_marker(html), "the reader still loads and shows its own not-found"
        assert _head_values(html)["canonicals"] == []

    def test_any_other_text_is_the_usual_shell(self, test_client, _blorp_is_missing):
        response = test_client.get("/text/Genesis.1")
        assert response.status_code == 200
        assert "X-Robots-Tag" not in response.headers
        assert _blorp_is_missing == ["Genesis 1"], "checked with the ref the reader asks for"

    async def test_the_404_comes_through_asgi(self, fastapi_client):
        response = await fastapi_client.get("/text/Blorp.4")
        assert response.status_code == 404
        assert _shell_marker(response.text)


SITE = "https://shelah-app.vercel.app"


class TestPageMeta:
    """Each URL declares itself, not the homepage: a canonical of ``/`` on a
    text page tells a crawler to fold that page into ``/``."""

    @pytest.mark.parametrize("path, canonical, title", [
        ("/text/Genesis.1", "/text/Genesis.1", "Genesis 1"),
        ("/text/Genesis%201", "/text/Genesis.1", "Genesis 1"),
        ("/text/Genesis+1", "/text/Genesis.1", "Genesis 1"),
        ("/text/Shulchan+Arukh,+Orach+Chayim+345:1",
         "/text/Shulchan_Arukh,_Orach_Chayim.345.1", "Shulchan Arukh, Orach Chayim 345:1"),
        ("/text/Shulchan%20Arukh,%20Orach%20Chayim%20345:1",
         "/text/Shulchan_Arukh,_Orach_Chayim.345.1", "Shulchan Arukh, Orach Chayim 345:1"),
        ("/text/Berakhot.2a.5", "/text/Berakhot.2a.5", "Berakhot 2a:5"),
        ("/text/a%2Fb", "/text/a%2Fb", "a/b"),
        ("/prayer/birkat%20hamazon", "/prayer/birkat_hamazon", "birkat hamazon"),
        ("/prayer/Upon_Arising", "/prayer/Upon_Arising", "Upon Arising"),
        ("/prayer/birkat+hamazon", "/prayer/birkat_hamazon", "birkat hamazon"),
        ("/calendar/2026-09-25", "/calendar/2026-09-25", "Jewish calendar 2026-09-25"),
        ("/community/Ashkenaz", "/community/Ashkenaz", "Ashkenaz Community Customs"),
        ("/community/Spanish%20and%20Portuguese", "/community/Spanish_and_Portuguese", "Spanish and Portuguese Community Customs"),
    ])
    def test_library_urls_declare_themselves(self, test_client, path, canonical, title):
        head = _head_values(test_client.get(path).get_data(as_text=True))
        assert head["canonicals"] == [SITE + canonical]
        assert head["og_url"] == SITE + canonical
        assert head["title"] == head["og_title"] == f"{title} · Sh&#39;elah"

    @pytest.mark.parametrize("path, location, title", [
        ("/?text=Genesis.1", "/text/Genesis.1", "Genesis 1"),
        ("/?prayer=Upon_Arising", "/prayer/Upon_Arising", "Upon Arising"),
        ("/?community=Sefardic", "/community/Sefardic", "Sefardic Community Customs"),
    ])
    def test_legacy_query_links_redirect_to_the_page_that_declares_itself(
        self, test_client, path, location, title,
    ):
        response = test_client.get(path)
        assert response.status_code == 308
        assert response.headers["Location"] == location
        head = _head_values(test_client.get(location).get_data(as_text=True))
        assert head["canonicals"] == [SITE + location]
        assert head["og_url"] == SITE + location
        assert head["title"] == f"{title} · Sh&#39;elah"

    @pytest.mark.parametrize("path", ["/", "/settings", "/profile", "/?lang=he", "/calendar/not-a-date", "/text/_", "/prayer/_", "/community/_"])
    def test_home_keeps_the_site_title_and_root_canonical(self, test_client, path):
        head = _head_values(test_client.get(path).get_data(as_text=True))
        assert head["canonicals"] == [SITE + "/"]
        assert head["title"] == "Sh&#39;elah - Torah Encyclopedia"

    @pytest.mark.parametrize("path", PRIVATE_PATHS + ["/?chat=abc", "/?text=Genesis.1&chat=x", "/?history=1", "/?conversation=abc"])
    def test_private_views_have_no_canonical(self, test_client, path):
        head = _head_values(test_client.get(path).get_data(as_text=True))
        assert head["canonicals"] == []
        assert head["title"] == "Sh&#39;elah - Torah Encyclopedia"

    def test_a_shared_answer_previews_as_its_own_url(self, test_client):
        head = _head_values(test_client.get("/a/Zx9_-abcDEF0123456789q").get_data(as_text=True))
        assert head["og_url"] == SITE + "/a/Zx9_-abcDEF0123456789q"

    def test_ref_text_is_escaped(self, test_client):
        html = test_client.get('/text/%22%3E%3Cscript%3Ex%3C%2Fscript%3E').get_data(as_text=True)
        assert "<script>x</script>" not in html.split("</head>", 1)[0]

    def test_python_slugs_match_router_js(self):
        """backend/page_meta.py mirrors router.js refToSlug/slugToRef."""
        from backend.page_meta import ref_to_slug, slug_to_ref
        pairs = [
            ("Genesis 2", "Genesis.2"),
            ("Shulchan Arukh, Orach Chayim 345:1", "Shulchan_Arukh,_Orach_Chayim.345.1"),
            ("Berakhot 2a:5", "Berakhot.2a.5"),
            ("Genesis 1:1-2:3", "Genesis.1.1-2.3"),
            ("Pirkei Avot", "Pirkei_Avot"),
        ]
        for ref, slug in pairs:
            assert ref_to_slug(ref) == slug
            assert slug_to_ref(slug) == ref


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


class TestLegacyQueryRedirect:
    """``/?text=`` / ``?prayer=`` / ``?community=`` 308 to their path form, with
    the same precedence page_meta uses for the canonical tag."""

    @pytest.mark.parametrize("path, location", [
        ("/?prayer=Upon_Arising&text=Genesis.1", "/text/Genesis.1"),
        ("/?community=Sefardic&prayer=Upon_Arising", "/prayer/Upon_Arising"),
        ("/?prayer=birkat%20hamazon", "/prayer/birkat_hamazon"),
        ("/?text=Shulchan+Arukh,+Orach+Chayim+345:1", "/text/Shulchan_Arukh,_Orach_Chayim.345.1"),
        ("/?text=Genesis.1&lang=he&layout=parallel", "/text/Genesis.1?lang=he&layout=parallel"),
    ])
    def test_precedence_normalisation_and_other_params_are_kept(self, test_client, path, location):
        response = test_client.get(path)
        assert response.status_code == 308
        assert response.headers["Location"] == location

    @pytest.mark.parametrize("path", ["/?text=Genesis.1&chat=x", "/?text=", "/?lang=he", "/"])
    def test_private_empty_or_unrelated_queries_stay_on_the_shell(self, test_client, path):
        assert test_client.get(path).status_code == 200

    def test_head_redirects_and_post_is_left_alone(self, test_client):
        assert test_client.head("/?text=Genesis.1").status_code == 308
        assert test_client.post("/?text=Genesis.1").status_code != 308

    def test_only_the_root_path_is_redirected(self, test_client):
        assert test_client.get("/about?text=Genesis.1").status_code != 308

    @pytest.mark.parametrize("path, canonical", [
        ("/settings?text=Genesis.1", "/text/Genesis.1"),
        ("/profile?prayer=Havdalah", "/prayer/Havdalah"),
        ("/settings?community=Ashkenaz", "/community/Ashkenaz"),
    ])
    def test_the_other_shell_routes_keep_the_query_canonical(self, test_client, path, canonical):
        """/settings and /profile serve the same shell as / but aren't
        redirected, so page_meta.query_meta still names the canonical --
        the same path the redirect on / would have gone to."""
        response = test_client.get(path)
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert f'<link rel="canonical" href="https://shelah-app.vercel.app{canonical}">' in html


class TestSiddurPaths:
    """/siddur/<rite>[/<service>[/<section>]] (backend/routes_spa_paths.py
    siddur_page): every page is known in advance, so a missing one is a real
    404; each gets its own title and canonical (page_meta.siddur_meta)."""

    @pytest.mark.parametrize("path, title, canonical", [
        ("/siddur/edot-hamizrach", "Siddur · Sephardi (Edot HaMizrach)", "/siddur/edot-hamizrach"),
        ("/siddur/edot-hamizrach/shacharit", "Shacharit", "/siddur/edot-hamizrach/shacharit"),
        ("/siddur/edot-hamizrach/shacharit/amida", "Amida · Shacharit", "/siddur/edot-hamizrach/shacharit/amida"),
        ("/siddur/edot-hamizrach/birkat-hamazon", "Birkat Hamazon", "/siddur/edot-hamizrach/birkat-hamazon"),
        # An overlay after the page: the page is still the page.
        ("/siddur/edot-hamizrach/mincha/chat/new", "Mincha", "/siddur/edot-hamizrach/mincha"),
        ("/siddur/edot-hamizrach/arbit/calendar/2026-09-27", "Arbit", "/siddur/edot-hamizrach/arbit"),
    ])
    def test_pages_declare_themselves(self, test_client, path, title, canonical):
        response = test_client.get(path)
        assert response.status_code == 200
        head = _head_values(response.get_data(as_text=True))
        assert head["canonicals"] == [SITE + canonical]
        assert head["title"] == f"{title} · Sh&#39;elah"

    def test_bare_siddur_redirects_to_the_default_rite_keeping_the_query(self, test_client):
        response = test_client.get("/siddur?lang=he")
        assert response.status_code == 308
        assert response.headers["Location"] == "/siddur/edot-hamizrach?lang=he"
        assert test_client.get("/siddur").headers["Location"] == "/siddur/edot-hamizrach"

    def test_a_trailing_slash_redirects(self, test_client):
        response = test_client.get("/siddur/edot-hamizrach/shacharit/")
        assert response.status_code == 308
        assert response.headers["Location"] == "/siddur/edot-hamizrach/shacharit"

    @pytest.mark.parametrize("path", [
        "/siddur/ashkenaz",
        "/siddur/edot-hamizrach/no-such-service",
        "/siddur/edot-hamizrach/shacharit/no-such-section",
        "/siddur/edot-hamizrach/shacharit/amida/extra",
        # A one-section service has no separate section page.
        "/siddur/edot-hamizrach/birkat-hamazon/birkat-hamazon",
        "/siddur/edot-hamizrach/mincha/sefardic",  # a community with no conversation
    ])
    def test_pages_the_siddur_lacks_are_404(self, test_client, path):
        assert test_client.get(path).status_code == 404

    def test_the_sitemap_lists_every_siddur_page(self, test_client):
        from backend import siddur_data

        body = test_client.get("/sitemap.xml").get_data(as_text=True)
        services = list(siddur_data.iter_services())
        sections = sum(len(s["sections"]) for _, s in services if len(s["sections"]) > 1)
        assert body.count(f"<loc>{SITE}/siddur/") == 1 + len(services) + sections
        assert f"<loc>{SITE}/siddur/edot-hamizrach/shacharit/amida</loc>" in body
        assert f"<loc>{SITE}/siddur/edot-hamizrach/birkat-hamazon/birkat-hamazon</loc>" not in body
