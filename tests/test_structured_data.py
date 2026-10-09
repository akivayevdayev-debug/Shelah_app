"""
Tests for the SEO/structured-data audit: the
og:image/twitter:image assets and JSON-LD blocks across the public templates.

Scope note: STABLE_PAGES covers the product and legal pages. `/`
(index.html) is deliberately not in the list: its og-image/JSON-LD markup
was never separated out from the rest of that large template when this test
was written. Extend STABLE_PAGES with "/" once that markup has its own
coverage.
"""

import json
import re

from PIL import Image

STABLE_PAGES = [
    "/about", "/help", "/glossary",
    "/ai-disclosure", "/acceptable-use", "/dmca", "/licenses",
    "/accessibility", "/privacy", "/terms",
]

LD_JSON_RE = re.compile(r'<script type="application/ld\+json">\n(.*?)\n\s*</script>', re.S)


class TestSocialShareImage:
    def test_all_pages_reference_new_og_image(self, test_client):
        for path in STABLE_PAGES:
            html = test_client.get(path).get_data(as_text=True)
            assert "/static/og-image.png" in html, f"{path} does not reference the new OG image"
            assert "favicon-512.png" not in re.search(
                r'<meta property="og:image"[^>]*>', html
            ).group(0), f"{path} og:image still points at the old favicon"

    def test_all_pages_use_summary_large_image_card(self, test_client):
        for path in STABLE_PAGES:
            html = test_client.get(path).get_data(as_text=True)
            assert 'name="twitter:card" content="summary_large_image"' in html, path

    def test_og_image_is_landscape_1200x630(self):
        with Image.open("static/og-image.png") as im:
            assert im.size == (1200, 630)


class TestStructuredData:
    def test_every_page_has_at_least_one_ld_json_block(self, test_client):
        for path in STABLE_PAGES:
            html = test_client.get(path).get_data(as_text=True)
            blocks = LD_JSON_RE.findall(html)
            assert blocks, f"{path} has no application/ld+json block"
            for block in blocks:
                json.loads(block)  # raises if invalid JSON

    def test_legal_and_about_pages_have_webpage_block(self, test_client):
        for path in STABLE_PAGES:
            html = test_client.get(path).get_data(as_text=True)
            blocks = [json.loads(b) for b in LD_JSON_RE.findall(html)]
            webpages = [b for b in blocks if b.get("@type") == "WebPage"]
            assert len(webpages) == 1, f"{path} should have exactly one WebPage block"
            assert webpages[0]["url"] == f"https://shelah.org{path}"
            assert "&amp;" not in json.dumps(webpages[0])
