"""
Coverage for the calendar detail card's data: the `detail` object that
/api/holidays attaches to every Hebcal-sourced event.

Fixtures mirror real Hebcal `hebcal?v=1&cfg=json` items (fetched 2026-09-19):
a major yom tov with full leyning, a fast day, and a parasha whose `leyning`
also carries seven aliyot and a triennial cycle that must NOT be forwarded.
"""

from __future__ import annotations

from backend.routes_calendar import (
    _hebcal_detail,
    _hebcal_item_to_event,
    _safe_hebcal_link,
)

ROSH_HASHANA = {
    "title": "Rosh Hashana 5787",
    "date": "2026-09-12",
    "hdate": "1 Tishrei 5787",
    "category": "holiday",
    "subcat": "major",
    "yomtov": True,
    "hebrew": "ראש השנה 5787",
    "leyning": {
        "1": "Genesis 21:1-21:4",
        "torah": "Genesis 21:1-34; Numbers 29:1-6",
        "haftarah": "I Samuel 1:1-2:10",
        "maftir": "Numbers 29:1-29:6",
    },
    "link": "https://hebcal.com/h/rosh-hashana-2026?us=js&um=api",
    "memo": "The Jewish New Year. Also spelled Rosh Hashanah",
}

PARASHA = {
    "title": "Parashat Nitzavim-Vayeilech",
    "date": "2026-09-05",
    "hdate": "23 Elul 5786",
    "category": "parashat",
    "hebrew": "פרשת נצבים־וילך",
    "leyning": {
        "1": "Deuteronomy 29:9-29:28",
        "torah": "Deuteronomy 29:9-31:30",
        "haftarah": "Isaiah 61:10-63:9",
        "maftir": "Deuteronomy 31:28-31:30",
        "triennial": {"1": "Deuteronomy 29:9-29:11"},
    },
    "link": "https://hebcal.com/s/nitzavim-vayeilech-20260905?us=js&um=api",
}


class TestHebcalDetail:
    def test_yom_tov_detail_carries_description_dates_and_readings(self):
        detail = _hebcal_detail(ROSH_HASHANA)
        assert detail["memo"] == "The Jewish New Year. Also spelled Rosh Hashanah"
        assert detail["hebrew"] == "ראש השנה 5787"
        assert detail["hdate"] == "1 Tishrei 5787"
        assert detail["subcat"] == "major"
        assert detail["yomtov"] is True
        assert detail["leyning"] == {
            "torah": "Genesis 21:1-34; Numbers 29:1-6",
            "haftarah": "I Samuel 1:1-2:10",
            "maftir": "Numbers 29:1-29:6",
        }

    def test_leyning_drops_aliyot_and_triennial(self):
        """Payload guard: only torah/haftarah/maftir are forwarded."""
        readings = _hebcal_detail(PARASHA)["leyning"]
        assert set(readings) == {"torah", "haftarah", "maftir"}

    def test_missing_fields_are_omitted_not_null(self):
        detail = _hebcal_detail({"title": "Yom HaAliyah", "date": "2026-03-28"})
        assert detail == {}

    def test_blank_and_non_string_values_are_ignored(self):
        detail = _hebcal_detail(
            {"memo": "   ", "hebrew": 5, "leyning": {"torah": "", "haftarah": None}}
        )
        assert detail == {}

    def test_non_dict_leyning_is_ignored(self):
        assert "leyning" not in _hebcal_detail({"leyning": "Genesis 1:1"})

    def test_yomtov_only_when_literally_true(self):
        assert "yomtov" not in _hebcal_detail({"yomtov": "yes"})
        assert _hebcal_detail({"yomtov": True})["yomtov"] is True


class TestSafeHebcalLink:
    def test_accepts_hebcal_https_hosts(self):
        url = "https://hebcal.com/h/rosh-hashana-2026?us=js&um=api"
        assert _safe_hebcal_link(url) == url
        assert _safe_hebcal_link("https://www.hebcal.com/holidays/sukkot-2026")

    def test_rejects_other_hosts_schemes_and_lookalikes(self):
        for bad in (
            "http://hebcal.com/h/x",
            "https://evil.example/h/x",
            "https://hebcal.com.evil.example/h/x",
            "javascript:alert(1)",
            "//hebcal.com/h/x",
            "",
            None,
            42,
        ):
            assert _safe_hebcal_link(bad) is None, bad

    def test_bad_link_is_dropped_from_detail(self):
        detail = _hebcal_detail({"link": "https://evil.example/x", "memo": "Fast"})
        assert "link" not in detail
        assert detail["memo"] == "Fast"


class TestEventShape:
    def test_event_includes_detail_and_keeps_existing_keys(self):
        event = _hebcal_item_to_event(ROSH_HASHANA)
        assert event["start"] == "2026-09-12"
        assert event["category"] == "holiday"
        assert event["allDay"] is True
        assert event["detail"]["memo"].startswith("The Jewish New Year")

    def test_event_without_extras_has_empty_detail(self):
        event = _hebcal_item_to_event(
            {"title": "Yom HaAliyah", "date": "2026-03-28", "category": "holiday"}
        )
        assert event["detail"] == {}
