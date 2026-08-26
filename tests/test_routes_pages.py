"""
Tests for backend/routes_pages.py routes (plan.md §12.1, §12.2, §12.5.1).

Covers:
  - GET /about      -> HTML
  - GET /help       -> HTML
  - GET /glossary   -> HTML, renders glossary.json entries
  - GET /robots.txt -> text/plain, references sitemap
  - GET /sitemap.xml -> application/xml, lists stable public routes
"""


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
            assert f"<loc>https://shelah-app.vercel.app{path}</loc>" in body

    def test_sitemap_excludes_dynamic_routes(self, test_client):
        body = test_client.get("/sitemap.xml").get_data(as_text=True)
        assert "/ask" not in body
        assert "/api/" not in body


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
