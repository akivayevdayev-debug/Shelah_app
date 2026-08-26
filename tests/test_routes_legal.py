"""
Tests for backend/routes_legal.py routes.

Covers:
  - GET /ai-disclosure   → HTML (AI transparency/disclosure statement)
  - GET /acceptable-use  → HTML (acceptable use policy)
  - GET /dmca            → HTML (DMCA / copyright policy)
  - GET /licenses        → HTML (licenses & attribution page)

plan.md §8.A: these are the four new legal pages Prompt 12 adds alongside
the pre-existing terms/privacy/accessibility routes (still in app.py).
"""

import pytest


class TestAiDisclosureRoute:
    def test_ai_disclosure_returns_200(self, test_client):
        response = test_client.get("/ai-disclosure")
        assert response.status_code == 200

    def test_ai_disclosure_returns_html(self, test_client):
        response = test_client.get("/ai-disclosure")
        ct = response.content_type.lower()
        assert "text/html" in ct


class TestAcceptableUseRoute:
    def test_acceptable_use_returns_200(self, test_client):
        response = test_client.get("/acceptable-use")
        assert response.status_code == 200

    def test_acceptable_use_returns_html(self, test_client):
        response = test_client.get("/acceptable-use")
        ct = response.content_type.lower()
        assert "text/html" in ct


class TestDmcaRoute:
    def test_dmca_returns_200(self, test_client):
        response = test_client.get("/dmca")
        assert response.status_code == 200

    def test_dmca_returns_html(self, test_client):
        response = test_client.get("/dmca")
        ct = response.content_type.lower()
        assert "text/html" in ct


class TestLicensesRoute:
    def test_licenses_returns_200(self, test_client):
        response = test_client.get("/licenses")
        assert response.status_code == 200

    def test_licenses_returns_html(self, test_client):
        response = test_client.get("/licenses")
        ct = response.content_type.lower()
        assert "text/html" in ct


class TestLegalPagesCrossLinking:
    """Every legal page should link to every other legal page in its footer
    so a user landing on any one of them can reach the rest."""

    ALL_LEGAL_PATHS = [
        "/terms", "/privacy", "/ai-disclosure", "/acceptable-use",
        "/dmca", "/accessibility", "/licenses",
    ]

    @pytest.mark.parametrize("path", ALL_LEGAL_PATHS)
    def test_page_links_to_every_other_legal_page(self, test_client, path):
        response = test_client.get(path)
        html = response.get_data(as_text=True)
        for other in self.ALL_LEGAL_PATHS:
            assert f'href="{other}"' in html, f"{path} is missing a link to {other}"
