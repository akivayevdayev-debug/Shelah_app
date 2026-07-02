"""
Supplementary coverage tests for backend/routes_library.py, targeting the
specific branches tests/test_routes_library.py doesn't reach: the Talmud
daf-parsing/section-synthesis helpers inside library_leaf_refs, the
word-meaning English->Hebrew translation fallback, DOCX/PDF export
unavailability branches, word-wrap edge cases, and the search-suggest
liturgy/text-hit loops.
"""

from __future__ import annotations

import pytest


class TestLibraryLeafRefsTalmudSynthesis:
    def test_talmud_daf_synthesis_from_schema(self, test_client, monkeypatch):
        import backend.sefaria_library as sl
        monkeypatch.setattr(sl, "get_index_leaf_refs", lambda title, max_refs=120: [])
        monkeypatch.setattr(sl, "get_index_entry", lambda title: {
            "schema": {
                "lengths": [6],
                "sectionNames": ["Daf"],
                "addressTypes": ["Talmud"],
            },
        })
        response = test_client.get("/api/library/leaf-refs?title=Berakhot")
        assert response.status_code == 200
        body = response.get_json()
        assert body["refs"][0] == "Berakhot 2a"
        assert body["refs"][1] == "Berakhot 2b"

    def test_non_talmud_numbered_section_synthesis(self, test_client, monkeypatch):
        import backend.sefaria_library as sl
        monkeypatch.setattr(sl, "get_index_leaf_refs", lambda title, max_refs=120: [])
        monkeypatch.setattr(sl, "get_index_entry", lambda title: {
            "schema": {"lengths": [3], "sectionNames": ["Chapter"], "addressTypes": ["Integer"]},
        })
        response = test_client.get("/api/library/leaf-refs?title=Pirkei Avot")
        assert response.status_code == 200
        body = response.get_json()
        assert body["refs"] == ["Pirkei Avot 1", "Pirkei Avot 2", "Pirkei Avot 3"]

    def test_single_length_schema_yields_no_synthesized_refs(self, test_client, monkeypatch):
        import backend.sefaria_library as sl
        monkeypatch.setattr(sl, "get_index_leaf_refs", lambda title, max_refs=120: [])
        monkeypatch.setattr(sl, "get_index_entry", lambda title: {
            "schema": {"lengths": [1], "sectionNames": ["Chapter"]},
        })
        response = test_client.get("/api/library/leaf-refs?title=Short Work")
        assert response.status_code == 200
        body = response.get_json()
        assert body["refs"] == []

    def test_talmud_chapter_sections_extracted_and_collapse_applied(self, test_client, monkeypatch):
        import backend.sefaria_library as sl
        monkeypatch.setattr(
            sl, "get_index_leaf_refs",
            lambda title, max_refs=120: [f"Berakhot 2a:{i}" for i in range(1, 4)] + [f"Berakhot 2b:{i}" for i in range(1, 3)],
        )
        monkeypatch.setattr(sl, "get_index_entry", lambda title: {
            "title": "Berakhot",
            "alts": {
                "Chapters": {
                    "nodes": [
                        {"title": "Chapter 1; MeEimatai", "wholeRef": "Berakhot 2a:1-13a:15"},
                    ],
                },
            },
        })
        response = test_client.get("/api/library/leaf-refs?title=Berakhot")
        assert response.status_code == 200
        body = response.get_json()
        assert body["sections"][0]["label"] == "MeEimatai"
        assert body["sections"][0]["fromDaf"] == "2a"
        assert body["sections"][0]["toDaf"] == "13a"
        # Collapsed refs should dedupe to unique daf values.
        assert body["refs"] == ["Berakhot 2a", "Berakhot 2b"]

    def test_missing_title_returns_empty_shape(self, test_client):
        response = test_client.get("/api/library/leaf-refs")
        assert response.status_code == 200
        assert response.get_json() == {"title": "", "refs": [], "sections": []}

    def test_get_index_leaf_refs_exception_falls_back_to_synthesis(self, test_client, monkeypatch):
        import backend.sefaria_library as sl

        def _raise(title, max_refs=120):
            raise RuntimeError("upstream failure")

        monkeypatch.setattr(sl, "get_index_leaf_refs", _raise)
        monkeypatch.setattr(sl, "get_index_entry", lambda title: {
            "schema": {"lengths": [2], "sectionNames": ["Chapter"]},
        })
        response = test_client.get("/api/library/leaf-refs?title=Some Work")
        assert response.status_code == 200
        body = response.get_json()
        assert body["refs"] == ["Some Work 1", "Some Work 2"]


class TestWordMeaningTranslationFallback:
    def test_english_word_requested_in_hebrew_triggers_translation(self, test_client, monkeypatch):
        import backend.routes_library as routes_library_module
        monkeypatch.setattr(
            routes_library_module, "_lookup_english_word_meaning",
            lambda word: ("A day of rest.", "dictionary"),
        )
        monkeypatch.setattr(
            routes_library_module, "_translate_english_text_online",
            lambda text: ("יום מנוחה", "google"),
        )
        monkeypatch.setattr(
            routes_library_module, "_collect_word_meaning_alternatives",
            lambda **kw: [],
        )
        response = test_client.get("/api/word/meaning?word=rest&lang=he")
        assert response.status_code == 200
        body = response.get_json()
        assert body["meaning"] == "יום מנוחה"
        assert "google" in body["source"]

    def test_translation_failure_keeps_original_meaning(self, test_client, monkeypatch):
        import backend.routes_library as routes_library_module
        monkeypatch.setattr(
            routes_library_module, "_lookup_english_word_meaning",
            lambda word: ("A day of rest.", "dictionary"),
        )
        monkeypatch.setattr(
            routes_library_module, "_translate_english_text_online",
            lambda text: ("", ""),
        )
        monkeypatch.setattr(
            routes_library_module, "_collect_word_meaning_alternatives",
            lambda **kw: [],
        )
        response = test_client.get("/api/word/meaning?word=rest&lang=he")
        assert response.status_code == 200
        body = response.get_json()
        assert body["meaning"] == "A day of rest."


class TestExportChapterUnavailableFormats:
    def test_docx_unavailable_returns_503(self, test_client, monkeypatch):
        import backend.routes_library as routes_library_module
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "docx":
                raise ImportError("docx not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        response = test_client.post("/api/export/chapter", json={
            "title": "Test", "ref": "Genesis 1:1", "format": "docx",
            "lines": [{"segment": "1", "he": "x", "en": "y"}],
        })
        assert response.status_code == 503
        assert "DOCX" in response.get_json()["error"]

    def test_pdf_unavailable_returns_503(self, test_client, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name in ("reportlab.lib.pagesizes", "reportlab.pdfgen"):
                raise ImportError("reportlab not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        response = test_client.post("/api/export/chapter", json={
            "title": "Test", "ref": "Genesis 1:1", "format": "pdf",
            "lines": [{"segment": "1", "he": "x", "en": "y"}],
        })
        assert response.status_code == 503
        assert "PDF" in response.get_json()["error"]

    def test_pdf_export_with_many_lines_triggers_page_break(self, test_client):
        long_lines = [
            {"segment": str(i), "he": "טקסט ארוך " * 10, "en": "long text " * 10}
            for i in range(1, 60)
        ]
        response = test_client.post("/api/export/chapter", json={
            "title": "Long Chapter", "ref": "Genesis 1", "format": "pdf", "lines": long_lines,
        })
        assert response.status_code == 200
        assert response.content_type == "application/pdf"


class TestSearchSuggestLoops:
    def test_liturgy_match_included(self, test_client, monkeypatch):
        import backend.sefaria_library as sl
        monkeypatch.setattr(sl, "get_liturgy_books", lambda max_items=120: [{"title": "Weekday Siddur"}])
        monkeypatch.setattr(sl, "search_library", lambda q, size=10, metadata_filters=None: [])
        response = test_client.get("/api/search/suggest?q=weekday")
        assert response.status_code == 200
        body = response.get_json()
        assert any(item["type"] == "prayer" for item in body)

    def test_text_search_hits_included_with_categories(self, test_client, monkeypatch):
        import backend.sefaria_library as sl
        monkeypatch.setattr(sl, "get_liturgy_books", lambda max_items=120: [])
        monkeypatch.setattr(sl, "search_library", lambda q, size=10, metadata_filters=None: [
            {"ref": "Genesis 1:1", "heRef": "בראשית א:א", "categories": ["Tanakh", "Torah"]},
        ])
        response = test_client.get("/api/search/suggest?q=genesis")
        assert response.status_code == 200
        body = response.get_json()
        matched = [item for item in body if item["type"] == "text" and item["value"] == "Genesis 1:1"]
        assert matched
        assert matched[0]["subtitle"] == "Tanakh > Torah"

    def test_ask_suggestion_always_present(self, test_client, monkeypatch):
        import backend.sefaria_library as sl
        monkeypatch.setattr(sl, "get_liturgy_books", lambda max_items=120: [])
        monkeypatch.setattr(sl, "search_library", lambda q, size=10, metadata_filters=None: [])
        response = test_client.get("/api/search/suggest?q=random query")
        body = response.get_json()
        assert any(item["type"] == "ask" for item in body)
