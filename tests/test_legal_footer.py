"""The seven legal pages share templates/components/legal_footer.html.

The partial emphasises the footer link for the page being viewed, driven by a
`current` variable each page sets via {% with current='<page>' %}. Jinja treats
an unset or mistyped `current` as an ordinary falsy value, so a page that
forgets it (or misspells it) would render a footer with nothing emphasised and
no error. These tests make that failure loud.
"""

from __future__ import annotations

import re

import pytest

LEGAL_PAGES = ["terms", "privacy", "ai-disclosure", "acceptable-use", "dmca", "accessibility", "licenses"]

_LINK = re.compile(r'<a href="(?P<href>[^"]*)"(?P<attrs>[^>]*)>', re.DOTALL)


def _footer_links(html: str):
    footer = re.search(r"<footer\b.*?</footer>", html, re.DOTALL)
    assert footer, "page has no <footer>"
    return [(m.group("href"), m.group("attrs")) for m in _LINK.finditer(footer.group(0))]


@pytest.mark.parametrize("page", LEGAL_PAGES)
def test_footer_emphasises_exactly_the_current_page(test_client, page):
    response = test_client.get(f"/{page}")
    assert response.status_code == 200

    links = _footer_links(response.get_data(as_text=True))
    emphasised = [href for href, attrs in links if "font-medium" in attrs]
    assert emphasised == [f"/{page}"]

    # The emphasised link also uses the heading colour, the others the secondary one.
    for href, attrs in links:
        if href in {f"/{p}" for p in LEGAL_PAGES}:
            expected = "--ink-heading" if href == f"/{page}" else "--ink-secondary"
            assert f"color: var({expected})" in attrs, (page, href)


@pytest.mark.parametrize("page", LEGAL_PAGES)
def test_footer_links_to_every_legal_page_and_home(test_client, page):
    links = _footer_links(test_client.get(f"/{page}").get_data(as_text=True))
    hrefs = [href for href, _ in links]
    assert hrefs == ["/"] + [f"/{p}" for p in ("terms", "privacy", "ai-disclosure",
                                              "acceptable-use", "dmca", "accessibility", "licenses")]


def test_footer_shows_the_current_year_in_text_and_both_languages(test_client):
    import datetime

    year = datetime.date.today().year
    html = test_client.get("/terms").get_data(as_text=True)
    # The visible text (not just the data-en attribute, which repeats the sentence).
    assert re.search(rf">©\s*Sh'elah {year}\. All rights reserved\.</p>", html)
    assert f'data-en="© Sh\'elah {year}. All rights reserved."' in html
    assert f'data-he="© Sh\'elah {year}. כל הזכויות שמורות."' in html
