"""
Tests for backend/routes_pages.py routes.

Covers:
  - GET /about      -> HTML
  - GET /help       -> HTML
  - GET /glossary   -> HTML, renders glossary.json entries
  - GET /robots.txt -> text/plain, references sitemap
  - GET /sitemap.xml -> application/xml, lists stable public routes
"""

from pathlib import Path


class TestAboutRoute:
    def test_about_returns_200(self, test_client):
        response = test_client.get("/about")
        assert response.status_code == 200

    def test_about_returns_html(self, test_client):
        response = test_client.get("/about")
        assert "text/html" in response.content_type.lower()

    def test_about_bilingual(self, test_client):
        html = test_client.get("/about").get_data(as_text=True)
        assert 'data-en="About Sh' in html or "About Sh'elah" in html
        assert "data-he=" in html


class TestHelpRoute:
    def test_help_returns_200(self, test_client):
        response = test_client.get("/help")
        assert response.status_code == 200

    def test_help_returns_html(self, test_client):
        response = test_client.get("/help")
        assert "text/html" in response.content_type.lower()


class TestGlossaryRoute:
    def test_glossary_returns_200(self, test_client):
        response = test_client.get("/glossary")
        assert response.status_code == 200

    def test_glossary_returns_html(self, test_client):
        response = test_client.get("/glossary")
        assert "text/html" in response.content_type.lower()

    def test_glossary_renders_terms(self, test_client):
        html = test_client.get("/glossary").get_data(as_text=True)
        assert "Kezayit" in html
        assert "Muktzeh" in html


class TestRobotsTxtRoute:
    def test_robots_returns_200(self, test_client):
        response = test_client.get("/robots.txt")
        assert response.status_code == 200

    def test_robots_is_plain_text(self, test_client):
        response = test_client.get("/robots.txt")
        assert "text/plain" in response.content_type.lower()

    def test_robots_disallows_api(self, test_client):
        body = test_client.get("/robots.txt").get_data(as_text=True)
        assert "Disallow: /api/" in body

    def test_robots_references_sitemap(self, test_client):
        body = test_client.get("/robots.txt").get_data(as_text=True)
        assert "Sitemap: " in body
        assert "sitemap.xml" in body


class TestSitemapXmlRoute:
    def test_sitemap_returns_200(self, test_client):
        response = test_client.get("/sitemap.xml")
        assert response.status_code == 200

    def test_sitemap_is_xml(self, test_client):
        response = test_client.get("/sitemap.xml")
        assert "xml" in response.content_type.lower()

    def test_sitemap_lists_stable_public_routes(self, test_client):
        body = test_client.get("/sitemap.xml").get_data(as_text=True)
        for path in ("/about", "/help", "/glossary", "/terms", "/privacy"):
            assert f"<loc>https://shelah.org{path}</loc>" in body

    def test_sitemap_excludes_dynamic_routes(self, test_client):
        body = test_client.get("/sitemap.xml").get_data(as_text=True)
        assert "/ask" not in body
        assert "/api/" not in body
        assert "/answer/" not in body
        assert "/history" not in body

    def test_sitemap_lists_the_library_pages(self, test_client):
        import re

        body = test_client.get("/sitemap.xml").get_data(as_text=True)
        locs = re.findall(r"<loc>(.*?)</loc>", body)
        base = "https://shelah.org"
        texts = [loc for loc in locs if loc.startswith(f"{base}/text/")]
        assert len(texts) == 929, "every Tanakh chapter, once"
        for path in (
            "/text/Genesis.1", "/text/Genesis.50", "/text/I_Samuel.31",
            "/text/Song_of_Songs.8", "/text/II_Chronicles.36",
            "/prayer/Weekday_Shacharit", "/prayer/Havdalah",
            "/community/Ashkenaz", "/community/Greek-Romaniote",
        ):
            assert f"{base}{path}" in locs, path
        assert f"{base}/text/Genesis.51" not in locs
        assert len(locs) == len(set(locs)), "no URL listed twice"

    def test_each_library_loc_is_its_pages_own_canonical(self, test_client):
        """A sitemap URL that isn't the page's canonical is a mixed signal:
        every prayer and community, and a spread of chapters, are served and
        point their canonical at exactly the listed URL."""
        import re

        body = test_client.get("/sitemap.xml").get_data(as_text=True)
        base = "https://shelah.org"
        locs = [loc for loc in re.findall(r"<loc>(.*?)</loc>", body)
                if loc.startswith((f"{base}/text/", f"{base}/prayer/", f"{base}/community/"))]
        sample = [loc for loc in locs if "/text/" not in loc] + [
            loc for loc in locs if "/text/" in loc][::60]
        for loc in sample:
            response = test_client.get(loc[len(base):])
            assert response.status_code == 200, loc
            html = response.get_data(as_text=True)
            assert f'<link rel="canonical" href="{loc}">' in html, loc

    def test_tanakh_chapter_table_matches_the_readers_grid(self):
        """routes_pages._TANAKH_CHAPTERS mirrors CHAPTER_GRID_BOOKS in
        templates/index.html; the two can't drift apart."""
        import re
        from pathlib import Path

        from backend.routes_pages import _TANAKH_CHAPTERS

        html = (Path(__file__).resolve().parent.parent / "templates" / "index.html").read_text(encoding="utf-8")
        block = re.search(r"const CHAPTER_GRID_BOOKS = Object\.freeze\(\{(.*?)\}\);", html, re.S).group(1)
        grid = [(name.strip("'\""), int(count))
                for name, count in re.findall(r"^\s*('[^']+'|\w+):\s*(\d+),", block, re.M)]
        assert grid == list(_TANAKH_CHAPTERS)


class TestLlmsTxtRoute:
    def test_llms_txt_returns_200(self, test_client):
        response = test_client.get("/llms.txt")
        assert response.status_code == 200

    def test_llms_txt_is_plain_text(self, test_client):
        response = test_client.get("/llms.txt")
        assert "text/plain" in response.content_type.lower()

    def test_llms_txt_is_utf8_text_not_html(self, test_client):
        response = test_client.get("/llms.txt")
        assert response.headers["Content-Type"] == "text/plain; charset=utf-8"
        assert "<html" not in response.get_data(as_text=True).lower()

    def test_llms_txt_is_a_markdown_map_of_links(self, test_client):
        """llmstxt.org shape, which is what Lighthouse's agentic-browsing audit
        reads: one H1, then `- [title](url): note` list items. A bare URL is
        not a link to it ("File does not appear to contain any links")."""
        import re

        body = test_client.get("/llms.txt").get_data(as_text=True)
        lines = body.splitlines()
        assert lines[0] == "# Sh'elah"
        assert sum(1 for line in lines if line.startswith("# ")) == 1
        bullets = [line for line in lines if line.startswith("- ")]
        assert bullets
        link = re.compile(r"^- \[[^\]]+\]\(https://shelah\.org/[^)\s]*\): \S")
        assert all(link.match(line) for line in bullets), [line for line in bullets if not link.match(line)]

    def test_llms_txt_uses_only_the_canonical_host(self, test_client):
        body = test_client.get("/llms.txt").get_data(as_text=True)
        assert "shelah-app.vercel.app" not in body
        assert "http://" not in body

    def test_llms_txt_links_every_site_page_and_the_sitemap(self, test_client):
        """llms.txt and /sitemap.xml share _SITEMAP_PATHS: assert the site's own
        pages (the sitemap minus its ~950 library pages) are all linked, so the
        two cannot silently drift apart."""
        import re

        llms_body = test_client.get("/llms.txt").get_data(as_text=True)
        sitemap_body = test_client.get("/sitemap.xml").get_data(as_text=True)

        llms_urls = set(re.findall(r"\]\((https://[^)]+)\)", llms_body))
        sitemap_urls = set(re.findall(r"<loc>(.*?)</loc>", sitemap_body))
        library = re.compile(r"^https://shelah\.org/(text|prayer|community|siddur)/")
        site_pages = {url for url in sitemap_urls if not library.match(url)}

        assert site_pages <= llms_urls
        assert "https://shelah.org/sitemap.xml" in llms_urls
        assert site_pages  # guards against both sides silently going blank

    def test_llms_txt_library_links_are_real_pages(self, test_client):
        """Each library entry point is a page the sitemap also lists."""
        import re

        llms_body = test_client.get("/llms.txt").get_data(as_text=True)
        sitemap_urls = set(re.findall(r"<loc>(.*?)</loc>", test_client.get("/sitemap.xml").get_data(as_text=True)))
        library = re.compile(r"^https://shelah\.org/(text|prayer|community|siddur)/")
        linked = [u for u in re.findall(r"\]\((https://[^)]+)\)", llms_body) if library.match(u)]
        assert linked
        assert set(linked) <= sitemap_urls

    def test_every_sitemap_page_has_an_llms_entry(self):
        from backend.routes_pages import _LLMS_PAGES, _SITEMAP_PATHS

        assert {path for path, _c, _p in _SITEMAP_PATHS} == set(_LLMS_PAGES)

    def test_linked_pages_exist(self, test_client):
        """What llms.txt points at answers 200 (the report's "every URL has a
        successful canonical response", checked against the app itself)."""
        import re

        body = test_client.get("/llms.txt").get_data(as_text=True)
        for path in re.findall(r"\]\(https://shelah\.org(/[^)]*)\)", body):
            assert test_client.get(path).status_code == 200, path


class TestNewPagesLinkToLegalFooter:
    """about/help/glossary should each link out to the legal pages, matching
    the cross-linking convention tests/test_routes_legal.py enforces for the
    legal pages themselves."""

    NEW_PAGES = ["/about", "/help", "/glossary"]
    LEGAL_PATHS = [
        "/terms", "/privacy", "/ai-disclosure", "/acceptable-use",
        "/dmca", "/accessibility", "/licenses",
    ]

    def test_each_new_page_links_to_terms_and_privacy(self, test_client):
        for page in self.NEW_PAGES:
            html = test_client.get(page).get_data(as_text=True)
            for legal_path in ("/terms", "/privacy"):
                assert f'href="{legal_path}"' in html, f"{page} is missing a link to {legal_path}"


class TestGlossaryDataFallback:
    """/glossary must still render (with no entries) when its data file is
    unreadable or malformed, rather than erroring."""

    @staticmethod
    def _serve_glossary_from(monkeypatch, opener):
        import backend.routes_pages as routes_pages_module

        # Shadows the builtin only inside routes_pages; Jinja's template
        # loading (a different module) keeps using the real open().
        monkeypatch.setattr(routes_pages_module, "open", opener, raising=False)

    def test_an_unreadable_data_file_renders_an_empty_glossary(self, test_client, monkeypatch):
        def unreadable(*args, **kwargs):
            raise OSError("permission denied")

        self._serve_glossary_from(monkeypatch, unreadable)

        response = test_client.get("/glossary")

        assert response.status_code == 200
        assert "Kezayit" not in response.get_data(as_text=True)

    def test_a_malformed_data_file_renders_an_empty_glossary(self, test_client, monkeypatch):
        import io

        self._serve_glossary_from(monkeypatch, lambda *args, **kwargs: io.StringIO("{not json"))

        response = test_client.get("/glossary")

        assert response.status_code == 200
        assert "Kezayit" not in response.get_data(as_text=True)


class TestCanonicalHost:
    def test_no_template_names_the_old_vercel_host(self):
        """The site's canonical host is page_meta.SITE_BASE_URL; the project's
        Vercel URL is not a page anyone should be sent to (or an agent told of)."""
        for template in (Path(__file__).resolve().parent.parent / "templates").rglob("*.html"):
            assert "vercel.app" not in template.read_text(encoding="utf-8"), template.name

    def test_every_static_page_canonical_is_on_the_canonical_host(self):
        import re

        from backend import page_meta

        for template in (Path(__file__).resolve().parent.parent / "templates").glob("*.html"):
            # (index.html's is filled in per URL from page_meta.canonical.)
            for url in re.findall(r'<link rel="canonical" href="(https?://[^"]+)"', template.read_text(encoding="utf-8")):
                assert url.startswith(page_meta.SITE_BASE_URL + "/") or url == page_meta.SITE_BASE_URL, (template.name, url)
