"""about.html, help.html, and glossary.html share
templates/components/site_footer.html, which cross-links the other two of
the trio (skipping whichever is the current page) before the standard legal
links. Driven by a `current` variable each page sets via {% with
current='<page>' %} -- Jinja treats an unset or mistyped `current` as an
ordinary falsy value, so a page that forgets it (or misspells it) would
render a footer that links to itself, or omits a sibling, with no error.
These tests make that failure loud, mirroring test_legal_footer.py's
coverage of the analogous seven-legal-page component.
"""

from __future__ import annotations

import re

import pytest

TRIO_PAGES = ["about", "help", "glossary"]
_LEGAL_LINKS = ["/terms", "/privacy", "/ai-disclosure", "/acceptable-use", "/dmca", "/accessibility", "/licenses"]

_LINK = re.compile(r'<a href="(?P<href>[^"]*)"(?P<attrs>[^>]*)>', re.DOTALL)


def _footer_links(html: str):
    footer = re.search(r"<footer\b.*?</footer>", html, re.DOTALL)
    assert footer, "page has no <footer>"
    return [(m.group("href"), m.group("attrs")) for m in _LINK.finditer(footer.group(0))]


@pytest.mark.parametrize("page", TRIO_PAGES)
def test_footer_links_to_the_other_two_of_the_trio_and_every_legal_page(test_client, page):
    links = _footer_links(test_client.get(f"/{page}").get_data(as_text=True))
    hrefs = [href for href, _ in links]

    siblings = [f"/{p}" for p in TRIO_PAGES if p != page]
    assert hrefs == ["/"] + siblings + _LEGAL_LINKS

    # The page never links to itself.
    assert f"/{page}" not in siblings


@pytest.mark.parametrize("page", TRIO_PAGES)
def test_terms_of_service_is_always_emphasised_regardless_of_current_page(test_client, page):
    links = _footer_links(test_client.get(f"/{page}").get_data(as_text=True))
    attrs_by_href = dict(links)

    assert "font-medium" in attrs_by_href["/terms"]
    assert "color: var(--ink-heading)" in attrs_by_href["/terms"]

    # Nothing else in the footer picks up the emphasis.
    for href, attrs in links:
        if href != "/terms":
            assert "font-medium" not in attrs, (page, href)


def test_only_about_shows_the_copyright_line(test_client):
    about_html = test_client.get("/about").get_data(as_text=True)
    help_html = test_client.get("/help").get_data(as_text=True)
    glossary_html = test_client.get("/glossary").get_data(as_text=True)

    assert "All rights reserved" in about_html
    assert "All rights reserved" not in help_html
    assert "All rights reserved" not in glossary_html
