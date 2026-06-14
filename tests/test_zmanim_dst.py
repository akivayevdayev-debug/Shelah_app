"""
Unit tests for DST boundary safety in backend/zmanim_engine.py.

Uses freezegun.freeze_time to fix the wall-clock date so the zmanim engine
computes times relative to the exact DST transition boundary.

For each test date we assert:
  - Result is a dict (no exception propagated as a raw error)
  - Top-level 'zmanim' key is present
  - Sunrise, Sunset, and Nightfall (Tzet HaKochavim) slots are not None
    inside the ISO time dict (metadata.zmanim_iso)
  - No "error" key in the result (or if present, no critical zmanim are None)
"""

from __future__ import annotations

import pytest
from freezegun import freeze_time

from backend.zmanim_engine import get_community_zmanim

# ─── DST boundary dates ───────────────────────────────────────────────────────

DST_CASES = [
    # (label, date_str, lat, lon, tz)
    (
        "US spring-forward 2024",
        "2024-03-10",
        40.7, -74.0,
        "America/New_York",
    ),
    (
        "US fall-back 2024",
        "2024-11-03",
        40.7, -74.0,
        "America/New_York",
    ),
    (
        "Israel spring-forward 2025",
        "2025-03-29",
        31.77, 35.21,
        "Asia/Jerusalem",
    ),
]

# IDs used in pytest output
DST_IDS = [case[0] for case in DST_CASES]


@pytest.mark.parametrize(
    "label,date_str,lat,lon,tz",
    DST_CASES,
    ids=DST_IDS,
)
class TestZmanimDSTBoundaries:

    def test_returns_dict(self, label, date_str, lat, lon, tz, mock_outbound_http):
        with freeze_time(date_str):
            result = get_community_zmanim(lat, lon, timezone_str=tz)
        assert isinstance(result, dict), (
            f"[{label}] Expected dict, got {type(result)}: {result}"
        )

    def test_has_zmanim_key(self, label, date_str, lat, lon, tz, mock_outbound_http):
        with freeze_time(date_str):
            result = get_community_zmanim(lat, lon, timezone_str=tz)
        assert "zmanim" in result, (
            f"[{label}] Missing 'zmanim' key. Keys present: {list(result.keys())}"
        )

    def test_has_metadata_key(self, label, date_str, lat, lon, tz, mock_outbound_http):
        with freeze_time(date_str):
            result = get_community_zmanim(lat, lon, timezone_str=tz)
        assert "metadata" in result, (
            f"[{label}] Missing 'metadata' key. Keys present: {list(result.keys())}"
        )

    def test_no_error_key(self, label, date_str, lat, lon, tz, mock_outbound_http):
        """Engine should not surface an 'error' key on valid DST boundary dates."""
        with freeze_time(date_str):
            result = get_community_zmanim(lat, lon, timezone_str=tz)
        assert "error" not in result, (
            f"[{label}] Unexpected error: {result.get('error')}\n"
            f"Trace: {result.get('trace', '')}"
        )

    def test_sunrise_not_none_in_iso(self, label, date_str, lat, lon, tz, mock_outbound_http):
        """Sunrise must be computable — never None — at a valid location."""
        with freeze_time(date_str):
            result = get_community_zmanim(lat, lon, timezone_str=tz)
        if "error" in result:
            pytest.skip(f"[{label}] Engine returned error: {result['error']}")
        iso_times = result.get("metadata", {}).get("zmanim_iso", {})
        assert iso_times.get("Sunrise") is not None, (
            f"[{label}] Sunrise is None in zmanim_iso"
        )

    def test_sunset_not_none_in_iso(self, label, date_str, lat, lon, tz, mock_outbound_http):
        with freeze_time(date_str):
            result = get_community_zmanim(lat, lon, timezone_str=tz)
        if "error" in result:
            pytest.skip(f"[{label}] Engine returned error: {result['error']}")
        iso_times = result.get("metadata", {}).get("zmanim_iso", {})
        assert iso_times.get("Sunset") is not None, (
            f"[{label}] Sunset is None in zmanim_iso"
        )

    def test_nightfall_not_none_in_iso(self, label, date_str, lat, lon, tz, mock_outbound_http):
        """Nightfall (3 Stars) / Tzet HaKochavim must be computable."""
        with freeze_time(date_str):
            result = get_community_zmanim(lat, lon, timezone_str=tz)
        if "error" in result:
            pytest.skip(f"[{label}] Engine returned error: {result['error']}")
        iso_times = result.get("metadata", {}).get("zmanim_iso", {})
        assert iso_times.get("Nightfall (3 Stars)") is not None, (
            f"[{label}] Nightfall (3 Stars) is None in zmanim_iso"
        )

    def test_sunrise_is_valid_iso_string(self, label, date_str, lat, lon, tz, mock_outbound_http):
        """Sunrise ISO string must be parseable and contain a date fragment."""
        with freeze_time(date_str):
            result = get_community_zmanim(lat, lon, timezone_str=tz)
        if "error" in result:
            pytest.skip(f"[{label}] Engine returned error: {result['error']}")
        iso_times = result.get("metadata", {}).get("zmanim_iso", {})
        sunrise_iso = iso_times.get("Sunrise")
        if sunrise_iso is None:
            pytest.skip(f"[{label}] Sunrise is None — skipping ISO format check")

        # Must contain the target date fragment
        assert date_str[:7] in str(sunrise_iso), (
            f"[{label}] Sunrise ISO '{sunrise_iso}' does not contain expected "
            f"year-month '{date_str[:7]}'"
        )

    def test_zmanim_display_values_are_strings(self, label, date_str, lat, lon, tz, mock_outbound_http):
        """All display zmanim values must be non-empty strings (e.g. '07:12 AM' or 'N/A')."""
        with freeze_time(date_str):
            result = get_community_zmanim(lat, lon, timezone_str=tz)
        if "error" in result:
            pytest.skip(f"[{label}] Engine returned error: {result['error']}")
        zmanim = result.get("zmanim", {})
        for key, value in zmanim.items():
            assert isinstance(value, str), (
                f"[{label}] zmanim['{key}'] is {type(value)}, expected str"
            )
            assert len(value) > 0, (
                f"[{label}] zmanim['{key}'] is an empty string"
            )
