"""The day's prayer guidance (backend/siddur_day.py), Sephardic custom.

The engine was checked against @hebcal/core over 1990-2099 (both Israel and
the Diaspora; hebcal is GPL and stays out of the repo): Hebrew dates, Rosh
Chodesh, Chanukah and public fasts agree on every day; every Tachanun /
Tzidkatcha / Hallel difference is a documented Sephardic custom or a known
hebcal quirk, and the dates below pin each rule."""

import datetime as dt

import pytest

from backend import siddur_day as sd


def g(date, il=False):
    return sd.day_guidance(dt.date.fromisoformat(date), il)


class TestTachanun:
    @pytest.mark.parametrize("date, reason", [
        ("2027-04-12", "5 Nisan"),
        ("2027-05-21", "Pesach Sheni (14 Iyar)"),
        ("2027-05-25", "Lag BaOmer"),
        ("2027-06-17", "12 Sivan"),
        ("2027-08-18", "15 Av"),
        ("2026-09-11", "Erev Rosh Hashana"),
        ("2026-09-20", "Erev Yom Kippur"),
        ("2026-10-13", "2 Cheshvan"),
        ("2029-01-31", "15 Shevat"),
        ("2027-02-22", "15 Adar I (leap year)"),
        ("2026-12-10", "Chanukah"),
        ("2026-12-11", "Rosh Chodesh Tevet"),
    ])
    def test_no_tachanun_days(self, date, reason):
        assert g(date)["tachanun"] == {"shacharit": False, "mincha": False}, reason

    def test_ordinary_weekday(self):
        assert g("2026-10-20")["tachanun"] == {"shacharit": True, "mincha": True}

    def test_not_at_mincha_before_a_no_tachanun_day(self):
        assert g("2026-10-23")["tachanun"] == {"shacharit": True, "mincha": False}  # Friday, 12 Cheshvan
        assert g("2028-12-12")["tachanun"] == {"shacharit": True, "mincha": False}  # 24 Kislev, eve of Chanukah

    @pytest.mark.parametrize("date", ["2026-09-10", "2028-09-28"])  # 28 Elul, 8 Tishrei
    def test_said_at_mincha_before_erev_rh_and_erev_yk(self, date):
        assert g(date)["tachanun"] == {"shacharit": True, "mincha": True}

    def test_shabbat_has_tzidkatcha_when_a_weekday_would_have_tachanun(self):
        assert g("2026-10-24")["tachanun"] == {"shacharit": False, "mincha": False, "tzidkatcha": True}
        # Shabbat Chanukah: no Tzidkatcha.
        assert g("2026-12-05")["tachanun"]["tzidkatcha"] is False

    def test_weekdays_have_no_tzidkatcha_key(self):
        assert "tzidkatcha" not in g("2026-10-20")["tachanun"]


class TestHallel:
    @pytest.mark.parametrize("date, il, kind", [
        ("2027-04-22", False, "full"),   # 15 Nisan
        ("2027-04-23", False, "full"),   # 16 Nisan, Diaspora
        ("2027-04-23", True, "half"),    # 16 Nisan, Israel
        ("2027-04-29", False, "half"),   # 22 Nisan, Diaspora
        ("2027-04-29", True, "none"),    # Isru Chag in Israel
        ("2027-06-11", False, "full"),   # Shavuot
        ("2026-10-03", False, "full"),   # Shemini Atzeret
        ("2026-10-04", False, "full"),   # Simchat Torah, Diaspora
        ("2026-12-10", False, "full"),   # Chanukah
        ("2026-10-11", False, "half"),   # Rosh Chodesh
        ("2027-05-21", False, "none"),   # Pesach Sheni
        ("2026-09-12", False, "none"),   # Rosh Hashana is not Rosh Chodesh Hallel
    ])
    def test_kind(self, date, il, kind):
        assert g(date, il)["hallel"]["kind"] == kind

    def test_no_beracha_over_half_hallel(self):
        assert g("2026-10-11")["hallel"] == {"kind": "half", "beracha": False}


class TestSeasons:
    def test_mashiv_starts_at_musaf_of_shemini_atzeret(self):
        assert g("2026-10-03")["gevurot"] == {"arbit": "morid", "shacharit": "morid", "musaf": "mashiv", "mincha": "mashiv"}

    def test_mashiv_ends_at_musaf_of_first_day_pesach(self):
        assert g("2027-04-22")["gevurot"] == {"arbit": "mashiv", "shacharit": "mashiv", "musaf": "morid", "mincha": "morid"}

    @pytest.mark.parametrize("date, season", [("2027-01-15", "mashiv"), ("2027-07-15", "morid"), ("2026-10-15", "mashiv")])
    def test_whole_day_seasons(self, date, season):
        assert set(g(date)["gevurot"].values()) == {season}

    @pytest.mark.parametrize("date, il, form", [
        ("2026-10-17", True, "barchenu"),       # 6 Cheshvan
        ("2026-10-18", True, "barech-alenu"),   # 7 Cheshvan, Israel
        ("2026-10-18", False, "barchenu"),
        ("2026-12-04", False, "barchenu"),      # its Arbit is the night of 3 December
        ("2026-12-05", False, "barech-alenu"),  # Arbit on the night of 4 December
        ("2027-12-05", False, "barchenu"),      # 2028 is a leap year: from the night of 5 December
        ("2027-12-06", False, "barech-alenu"),
        ("2027-03-01", False, "barech-alenu"),
        ("2027-04-21", False, "barech-alenu"),  # Erev Pesach, through Mincha
        ("2027-04-23", False, "barchenu"),
        ("2026-09-20", False, "barchenu"),
    ])
    def test_birkat_hashanim(self, date, il, form):
        assert g(date, il)["birkatHashanim"] == form

    @pytest.mark.parametrize("year, day", [(2019, 6), (2020, 5), (2023, 6), (2026, 5), (2027, 6), (2099, 5)])
    def test_diaspora_rain_start(self, year, day):
        assert sd.diaspora_rain_start(year) == dt.date(year, 12, day)


class TestOccasions:
    def test_shabbat_shuva(self):
        assert g("2026-09-19")["occasions"][:2] == ["shabbat", "shabbat-shuva"]

    def test_chol_hamoed_and_festival_flags(self):
        day = g("2026-09-29")  # 18 Tishrei
        assert {"sukkot", "chol-hamoed-sukkot"} <= set(day["occasions"])
        assert day["yaalehVeyavo"] and day["musaf"] and not day["alHanissim"]

    def test_israel_second_day_is_chol_hamoed(self):
        assert "chol-hamoed-pesach" in g("2027-04-23", True)["occasions"]
        assert "chol-hamoed-pesach" not in g("2027-04-23", False)["occasions"]

    def test_purim_and_shushan_purim(self):
        purim, shushan = g("2027-03-23"), g("2027-03-24")
        assert purim["alHanissim"] and not purim["alHanissimJerusalem"]
        assert shushan["alHanissimJerusalem"] and not shushan["alHanissim"]

    def test_fast_day(self):
        day = g("2026-09-14")  # Tzom Gedalia
        assert "fast" in day["occasions"] and day["aneinu"] and day["aseretYemeiTeshuva"]

    @pytest.mark.parametrize("date, fast", [
        ("2026-09-14", "fast-of-gedalya"),
        ("2026-12-20", "tenth-of-tevet"),
        ("2027-03-22", "fast-of-esther"),
        ("2027-07-22", "seventeenth-of-tammuz"),
        ("2027-08-12", "tisha-beav"),
    ])
    def test_fast_day_names_which_fast_it_is(self, date, fast):
        assert g(date)["fastDay"] == fast

    def test_fastDay_is_none_off_a_fast(self):
        assert g("2026-10-20")["fastDay"] is None
        assert g("2026-09-21")["fastDay"] is None  # Yom Kippur is not one of the public fasts

    def test_yom_kippur_has_musaf(self):
        day = g("2026-09-21")
        assert "yom-kippur" in day["occasions"] and day["musaf"] and "fast" not in day["occasions"]

    def test_ordinary_day(self):
        day = g("2026-10-20")
        assert day["occasions"] == [] and not day["musaf"] and not day["yaalehVeyavo"]

    def test_omer(self):
        assert g("2027-04-22")["omer"] == {"today": None, "tonight": 1}
        assert g("2027-06-10")["omer"] == {"today": 49, "tonight": None}

    def test_hebrew_date(self):
        assert g("2026-09-27")["hebrew"] == {"year": 5787, "month": 7, "day": 16, "monthName": "Tishrei", "he": "ט״ז תשרי תשפ״ז"}

    @pytest.mark.parametrize("date", ["1899-12-31", "2100-01-01"])
    def test_range(self, date):
        with pytest.raises(ValueError):
            g(date)
