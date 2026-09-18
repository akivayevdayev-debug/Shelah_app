"""
Tests for core / static routes in Sh'elah.

Covers:
  - GET /              → HTML (index page)
  - GET /terms         → HTML (terms of service)
  - GET /privacy       → HTML (privacy policy)
  - GET /accessibility → HTML (accessibility statement)
  - GET /manifest.webmanifest → JSON manifest
  - GET /service-worker.js    → JavaScript
  - GET /favicon.ico          → SVG image (or redirect)
"""



class TestIndexRoute:
    def test_index_returns_200(self, test_client):
        response = test_client.get("/")
        assert response.status_code == 200

    def test_index_returns_html(self, test_client):
        response = test_client.get("/")
        ct = response.content_type.lower()
        assert "text/html" in ct


class TestSentryBrowserIntegration:
    """plan.md §17 STEP 1/5: SENTRY_DSN_BROWSER must be a true no-op when
    unset (no script tag, no network call), and render the Sentry CDN
    <script>/SRI/config block when set — verified against the rendered
    index.html, not just the env var read."""

    def test_unset_dsn_emits_no_sentry_script_tag(self, test_client):
        response = test_client.get("/")
        html = response.get_data(as_text=True)
        assert "sentry" not in html.lower()
        assert "browser.sentry-cdn.com" not in html
        assert "__SHELAH_SENTRY_DSN__" not in html

    def test_set_dsn_emits_cdn_script_with_sri_and_config(self, monkeypatch, test_client):
        import app as flask_app_module

        monkeypatch.setattr(flask_app_module, "SENTRY_DSN_BROWSER", "https://fakekey@o1.ingest.us.sentry.io/1")
        monkeypatch.setattr(flask_app_module, "SENTRY_ENVIRONMENT", "preview")
        monkeypatch.setattr(flask_app_module, "SENTRY_RELEASE", "abc123def")

        response = test_client.get("/")
        html = response.get_data(as_text=True)

        assert "https://browser.sentry-cdn.com/10.69.0/bundle.min.js" in html
        assert 'integrity="sha384-' in html
        assert "/static/js/sentry-init.js" in html
        assert "fakekey@o1.ingest.us.sentry.io" in html
        assert '"preview"' in html
        assert '"abc123def"' in html

    def test_set_dsn_does_not_load_replay_bundle(self, monkeypatch, test_client):
        """Deviation 6 (plan.md §17.3): omit the Replay integration entirely
        rather than zeroing its sample rates — verified by pinning the
        plain errors-only bundle filename, not a replay/tracing variant."""
        import app as flask_app_module

        monkeypatch.setattr(flask_app_module, "SENTRY_DSN_BROWSER", "https://fakekey@o1.ingest.us.sentry.io/1")

        response = test_client.get("/")
        html = response.get_data(as_text=True)
        assert "bundle.replay" not in html
        assert "bundle.tracing" not in html


class TestSentryCsp:
    """plan.md §17 STEP 5: the Sentry CDN host must be allowed in script-src
    and the ingest host in connect-src, or the SDK fails silently (CSP
    blocks it with no visible error) — the worst failure mode for an error
    tracker. Guards the CSP wiring, not just the script tag."""

    def test_csp_allows_sentry_cdn_script_and_ingest_connect(self, test_client):
        response = test_client.get("/")
        csp = response.headers.get("Content-Security-Policy", "")
        assert "browser.sentry-cdn.com" in csp
        assert "o4511830797975553.ingest.us.sentry.io" in csp

        script_src = next(part for part in csp.split(";") if "script-src" in part)
        connect_src = next(part for part in csp.split(";") if "connect-src" in part)
        assert "browser.sentry-cdn.com" in script_src
        assert "o4511830797975553.ingest.us.sentry.io" in connect_src


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


class TestAccessibilityRoute:
    def test_accessibility_returns_200(self, test_client):
        response = test_client.get("/accessibility")
        assert response.status_code == 200

    def test_accessibility_returns_html(self, test_client):
        response = test_client.get("/accessibility")
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
