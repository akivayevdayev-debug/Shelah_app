"""
Master fixture file for Sh'elah Phase 2.6 offline test suite.

Sets up environment variables BEFORE any app import so all module-level reads
see the test values. Provides:
  - test_client  : Flask test client
  - fastapi_client : httpx.AsyncClient wrapping the FastAPI app
  - mock_sefaria_text : typical Sefaria text response
  - mock_ai_response  : typical AI answer response dict
  - dst_dates         : DST boundary tuples for parametrized zmanim tests
  - autouse fixture   : HTTP-level mocking of Sefaria / Hebcal / translation /
                        Anthropic / Gemini / Supabase outbound calls
"""

from __future__ import annotations

import os

# ── Set env vars BEFORE any app-level import ──────────────────────────────────
os.environ.setdefault("FLASK_ENV", "testing")
os.environ.setdefault("CLERK_ENFORCE_AUTH", "false")
os.environ.setdefault("SEFARIA_API", "https://mock.sefaria.org/api")
os.environ.setdefault("SEFARIA_V3_API", "https://mock.sefaria.org/api/v3")
os.environ.setdefault("SUPABASE_URL", "https://mock.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "mock-key")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "mock-service-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "mock-anthropic-key")
os.environ.setdefault("GEMINI_API_KEY", "mock-gemini-key")
os.environ.setdefault("LOG_LEVEL", "ERROR")
# Suppress rate limiter in tests
os.environ.setdefault("RATELIMIT_ENABLED", "false")

import json
import re
import pytest
import responses as responses_lib
import respx
import httpx

# ── Import apps (env vars are already set) ────────────────────────────────────
import app as flask_app_module
import asgi


# ─── Core client fixtures ─────────────────────────────────────────────────────

@pytest.fixture()
def test_client():
    """Flask test client with testing config enabled."""
    flask_app_module.app.config["TESTING"] = True
    flask_app_module.app.config["SECRET_KEY"] = "test-secret-key"
    with flask_app_module.app.test_client() as client:
        yield client


@pytest.fixture()
async def fastapi_client():
    """Async httpx client wrapping the FastAPI app for ASGI-layer tests."""
    transport = httpx.ASGITransport(app=asgi.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        yield client


# ─── Outbound HTTP mocking (autouse) ──────────────────────────────────────────

@pytest.fixture(autouse=True)
def mock_outbound_http():
    """
    Intercept all outbound HTTP calls made via the `requests` library so tests
    never hit the real network.  Covers: Sefaria, Hebcal, MyMemory translation.

    httpx-based calls (Anthropic, Gemini, Supabase) are handled separately by
    `mock_outbound_httpx`.
    """
    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        # ── Sefaria text API (catch-all, must be added before specific paths) ──
        rsps.add(
            responses_lib.GET,
            re.compile(r"https://mock\.sefaria\.org/api/.*"),
            json={
                "ref": "Genesis 1:1",
                "he": ["בְּרֵאשִׁית בָּרָא אֱלֹהִים אֵת הַשָּׁמַיִם וְאֵת הָאָרֶץ׃"],
                "text": ["In the beginning God created the heaven and the earth."],
                "type": "text",
                "book": "Genesis",
                "categories": ["Tanakh", "Torah"],
            },
            status=200,
        )

        # ── Sefaria library index ─────────────────────────────────────────────
        rsps.add(
            responses_lib.GET,
            re.compile(r"https://mock\.sefaria\.org/api/index.*"),
            json={"categories": [], "contents": []},
            status=200,
        )

        # ── Hebcal API ────────────────────────────────────────────────────────
        rsps.add(
            responses_lib.GET,
            re.compile(r"https://www\.hebcal\.com/.*"),
            json={
                "items": [
                    {
                        "title": "Candle lighting",
                        "date": "2024-03-08T17:51:00-05:00",
                        "category": "candles",
                    }
                ]
            },
            status=200,
        )

        # ── MyMemory translation ──────────────────────────────────────────────
        rsps.add(
            responses_lib.GET,
            re.compile(r"https://api\.mymemory\.translated\.net/.*"),
            json={
                "responseData": {"translatedText": "mock translation", "match": 1.0},
                "responseStatus": 200,
            },
            status=200,
        )

        # ── Google Translate (fallback) ───────────────────────────────────────
        # Real shape: payload[0] is a list of [translated_chunk, original_chunk,
        # ...] segments — see backend/helpers.py:_extract_google_translated_text.
        # A bare ["mock translation"] (previous mock) doesn't match that shape,
        # so it silently parsed to "" and every google-translate test fell
        # through to the mymemory fallback without anyone noticing.
        rsps.add(
            responses_lib.GET,
            re.compile(r"https://translate\.googleapis\.com/.*"),
            json=[[["mock translation", "source text", None, None, 0]], None, "auto"],
            status=200,
        )

        # ── Anthropic health probe ────────────────────────────────────────────
        rsps.add(
            responses_lib.GET,
            re.compile(r"https://api\.anthropic\.com/.*"),
            json={"data": []},
            status=200,
        )

        # ── Gemini health probe ───────────────────────────────────────────────
        rsps.add(
            responses_lib.GET,
            re.compile(r"https://generativelanguage\.googleapis\.com/.*"),
            json={"models": []},
            status=200,
        )

        yield rsps


@pytest.fixture(autouse=True)
def mock_outbound_httpx():
    """
    Intercept httpx-based outbound calls (Anthropic SDK, Gemini, Supabase REST).
    """
    with respx.mock(assert_all_mocked=False, assert_all_called=False) as mock:
        # ── Anthropic messages endpoint ───────────────────────────────────────
        mock.post(
            url__regex=r"https://api\.anthropic\.com/v1/messages.*"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "msg_mock",
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({
                                "answer": "Mock AI answer for testing.",
                                "confidence": 0.9,
                                "structured": None,
                            }),
                        }
                    ],
                    "model": "claude-sonnet-4-5",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                },
            )
        )

        # ── Gemini generateContent endpoint ──────────────────────────────────
        mock.post(
            url__regex=r"https://generativelanguage\.googleapis\.com/.*"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [{"text": "Mock Gemini summary."}],
                                "role": "model",
                            },
                            "finishReason": "STOP",
                        }
                    ]
                },
            )
        )

        # ── Supabase REST endpoints ───────────────────────────────────────────
        mock.get(
            url__regex=r"https://mock\.supabase\.co/.*"
        ).mock(
            return_value=httpx.Response(200, json=[])
        )
        mock.post(
            url__regex=r"https://mock\.supabase\.co/.*"
        ).mock(
            return_value=httpx.Response(200, json={"data": [], "error": None})
        )

        yield mock


# ─── Domain-specific fixtures ─────────────────────────────────────────────────

@pytest.fixture()
def mock_sefaria_text():
    """Typical Sefaria text response shape."""
    return {
        "ref": "Genesis 1:1",
        "he": ["בְּרֵאשִׁית בָּרָא אֱלֹהִים"],
        "en": ["In the beginning God created"],
        "type": "text",
        "book": "Genesis",
        "categories": ["Tanakh", "Torah"],
        "lines": [
            {"he": "בְּרֵאשִׁית בָּרָא אֱלֹהִים", "en": "In the beginning God created"}
        ],
    }


@pytest.fixture()
def mock_ai_response():
    """Typical AI answer response dict shape."""
    return {
        "answer": "This is a mock halachic ruling for testing purposes.",
        "confidence": 0.88,
        "sources": [
            {
                "ref": "Genesis 1:1",
                "title": "Genesis",
                "lines": [{"en": "In the beginning", "he": "בְּרֵאשִׁית"}],
            }
        ],
        "customs": [],
        "meta": {
            "mode": "balanced",
            "community_lens": "All",
            "source_count": 1,
            "custom_count": 0,
            "generated_at": 1700000000,
            "fallback": False,
        },
    }


@pytest.fixture()
def dst_dates():
    """
    DST boundary tuples: (date_str, lat, lon, tz_name).

    Covers US spring-forward, US fall-back, and Israel spring-forward
    transitions to verify zmanim calculations are DST-safe.
    """
    return [
        # US spring-forward: clocks jump from 2:00 → 3:00 AM
        ("2024-03-10", 40.7, -74.0, "America/New_York"),
        # US fall-back: clocks fall from 2:00 → 1:00 AM
        ("2024-11-03", 40.7, -74.0, "America/New_York"),
        # Israel spring-forward: clocks jump forward in late March
        ("2025-03-29", 31.77, 35.21, "Asia/Jerusalem"),
    ]
