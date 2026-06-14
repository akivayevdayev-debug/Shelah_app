"""
Tests for core / static routes in Sh'elah.

Covers:
  - GET /              → HTML (index page)
  - GET /terms         → HTML (terms of service)
  - GET /privacy       → HTML (privacy policy)
  - GET /manifest.webmanifest → JSON manifest
  - GET /service-worker.js    → JavaScript
  - GET /favicon.ico          → SVG image (or redirect)
"""

import pytest


class TestIndexRoute:
    def test_index_returns_200(self, test_client):
        response = test_client.get("/")
        assert response.status_code == 200

    def test_index_returns_html(self, test_client):
        response = test_client.get("/")
        ct = response.content_type.lower()
        assert "text/html" in ct


class TestTermsRoute:
    def test_terms_returns_200(self, test_client):
        response = test_client.get("/terms")
        assert response.status_code == 200

    def test_terms_returns_html(self, test_client):
        response = test_client.get("/terms")
        ct = response.content_type.lower()
        assert "text/html" in ct


class TestPrivacyRoute:
    def test_privacy_returns_200(self, test_client):
        response = test_client.get("/privacy")
        assert response.status_code == 200

    def test_privacy_returns_html(self, test_client):
        response = test_client.get("/privacy")
        ct = response.content_type.lower()
        assert "text/html" in ct


class TestManifest:
    def test_manifest_returns_200(self, test_client):
        response = test_client.get("/manifest.webmanifest")
        assert response.status_code == 200

    def test_manifest_content_type(self, test_client):
        response = test_client.get("/manifest.webmanifest")
        ct = response.content_type.lower()
        # Served as application/manifest+json or application/json
        assert "application/manifest+json" in ct or "application/json" in ct


class TestServiceWorker:
    def test_service_worker_returns_200(self, test_client):
        response = test_client.get("/service-worker.js")
        assert response.status_code == 200

    def test_service_worker_content_type(self, test_client):
        response = test_client.get("/service-worker.js")
        ct = response.content_type.lower()
        assert "javascript" in ct


class TestFavicon:
    def test_favicon_responds(self, test_client):
        response = test_client.get("/favicon.ico")
        # Expect 200 (SVG file) or a redirect to /static/favicon.svg
        assert response.status_code in (200, 301, 302)

    def test_favicon_content_type_or_redirect(self, test_client):
        response = test_client.get("/favicon.ico", follow_redirects=True)
        assert response.status_code == 200
        ct = response.content_type.lower()
        assert "svg" in ct or "image" in ct
