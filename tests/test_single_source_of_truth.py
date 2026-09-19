"""app.py re-exports a few helpers that used to be copy-pasted between it and
backend/. These tests pin that each now has exactly one definition, so a
future edit to one copy can't silently diverge from the other."""

from __future__ import annotations

import app as flask_app_module
import backend.customs as customs
import backend.helpers as helpers


def test_the_community_registry_has_a_single_definition():
    assert flask_app_module.COMMUNITIES is helpers.COMMUNITIES
    assert flask_app_module.COMMUNITY_ALIASES is helpers.COMMUNITY_ALIASES


def test_the_trusted_source_builder_has_a_single_definition():
    assert flask_app_module._build_trusted_custom_sources is customs._build_trusted_sources
    assert flask_app_module._collect_trusted_authority_candidates is customs._collect_trusted_authority_candidates
    assert flask_app_module._dedupe_source_labels is customs._dedupe_source_labels


def test_every_alias_resolves_to_a_registered_community():
    """A canonical name in the alias table that isn't in the registry would make
    _detect_community_in_text() return a community that has no data file."""
    assert set(helpers.COMMUNITY_ALIASES.values()) <= set(helpers.COMMUNITIES)


def test_detect_community_prefers_the_longest_alias():
    # "turkish ottoman sefardic" must win over its own substrings ("turkish", "sefardic").
    assert flask_app_module._detect_community_in_text(
        "What is the custom for turkish ottoman sefardic families?") == "Turkish-Ottoman"
    assert flask_app_module._detect_community_in_text("no community mentioned") is None
