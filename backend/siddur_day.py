"""What changes in the prayers on a given Hebrew day (Sephardic custom).

The "Today" guidance the Prayers redesign shows (research brief §3): which
seasonal and occasional texts apply, per service. Deterministic from the
Hebrew date and Israel/Diaspora -- no location, no user state -- so the
endpoint that serves it is a pure function of its URL and can be cached
publicly. The client decides WHICH Hebrew day it is (after local sunset it
asks for tomorrow's civil date), since only it knows the user's sunset.

``date`` is the civil date whose daytime is the Hebrew day asked about; that
Hebrew day began the evening before, so its Arbit is said on the previous
civil evening.

Rules, with the brief's sources:
- Tachanun (Sephardic list, Halachipedia / Peninei Halakha): not said on
  Shabbat, Rosh Chodesh, all of Nisan, Pesach Sheni, Lag BaOmer, 1-12 Sivan,
  Tisha B'Av, 15 Av, Erev Rosh Hashana, Rosh Hashana, Erev Yom Kippur, Yom
  Kippur, 11 Tishrei-2 Cheshvan, Chanukah, 15 Shevat, 14-15 Adar (both
  Adars in a leap year). Nor at the Mincha before any of those days --
  except the Mincha before Erev Rosh Hashana and before Erev Yom Kippur.
  Days outside the calendar (a mourner's house, a groom, a brit) can't be
  computed; the client says so.
- Hallel: full on Chanukah, the first day(s) of Pesach, Shavuot, Sukkot
  through Simchat Torah; half on Rosh Chodesh and the rest of Pesach.
  Sephardim say no beracha over half Hallel (Shulchan Aruch OC 422:2).
- Mashiv HaRuach from Musaf of Shemini Atzeret to Musaf of the first day of
  Pesach; Morid HaTal otherwise (Sephardim say it all summer). The switch
  is at Musaf, so the answer depends on the service.
- Barech Alenu (the winter form of Birkat HaShanim; Barechenu in summer):
  in Israel from Arbit of 7 Cheshvan; outside it from Arbit on the night of
  4 December, or 5 December when the next civil year is a leap year (valid
  1900-2099); both through Mincha of Erev Pesach.
- Ya'aleh VeYavo on Rosh Chodesh, Chol HaMoed and Yom Tov; Al HaNissim on
  Chanukah and Purim (15 Adar in Jerusalem); the Ten Days of Repentance
  1-10 Tishrei; Aneinu on public fasts; the Omer from the second night of
  Pesach to Shavuot eve.
"""

from __future__ import annotations

import datetime as dt

from pyluach import dates as heb_dates
from zmanim.hebrew_calendar.jewish_calendar import JewishCalendar

MIN_DATE = dt.date(1900, 1, 1)
MAX_DATE = dt.date(2099, 12, 31)

NISAN, IYAR, SIVAN, TAMMUZ, AV, ELUL, TISHREI, CHESHVAN, KISLEV, TEVET, SHEVAT, ADAR, ADAR_II = range(1, 14)
_PUBLIC_FASTS = {"tzom_gedalyah", "tenth_of_teves", "taanis_esther", "seventeen_of_tammuz", "tisha_beav"}
# Which public fast it is, named by the siddur's own "Taaniyot" section
# (data/siddur/*/services/taaniyot.json; Tisha B'Av has no section of its own).
_FAST_SLUGS = {
    "tzom_gedalyah": "fast-of-gedalya",
    "tenth_of_teves": "tenth-of-tevet",
    "taanis_esther": "fast-of-esther",
    "seventeen_of_tammuz": "seventeenth-of-tammuz",
    "tisha_beav": "tisha-beav",
}
SERVICES = ("arbit", "shacharit", "musaf", "mincha")


def _calendar(date: dt.date, il: bool) -> JewishCalendar:
    jc = JewishCalendar(date)
    jc.in_israel = il
    return jc


def _is_purim_adar(jc: JewishCalendar) -> bool:
    """The Adar Purim falls in: Adar, or Adar II in a leap year."""
    return jc.jewish_month == (ADAR_II if jc.is_jewish_leap_year() else ADAR)


def _no_tachanun_day(jc: JewishCalendar, *, count_shabbat: bool = True) -> bool:
    m, d = jc.jewish_month, jc.jewish_day
    if (count_shabbat and jc.day_of_week == 7) or jc.is_rosh_chodesh() or jc.is_chanukah():
        return True
    if m == NISAN:
        return True
    if m == IYAR and d in (14, 18):  # Pesach Sheni, Lag BaOmer
        return True
    if m == SIVAN and d <= 12:
        return True
    if jc.significant_day() == "tisha_beav" or (m == AV and d == 15):
        return True
    if m == ELUL and d == 29:
        return True
    if m == TISHREI and (d <= 2 or d >= 9):  # RH, Erev YK onward to month's end
        return True
    if m == CHESHVAN and d <= 2:
        return True
    if m == SHEVAT and d == 15:
        return True
    if m in (ADAR, ADAR_II) and d in (14, 15):
        return True
    return False


def _mincha_tachanun(today: JewishCalendar, tomorrow: JewishCalendar, *, count_shabbat: bool = True) -> bool:
    if _no_tachanun_day(today, count_shabbat=count_shabbat):
        return False
    # Said at Mincha the day before Erev Rosh Hashana / Erev Yom Kippur.
    t_m, t_d = tomorrow.jewish_month, tomorrow.jewish_day
    if (t_m == ELUL and t_d == 29) or (t_m == TISHREI and t_d == 9):
        return True
    return not _no_tachanun_day(tomorrow)


def tachanun(date: dt.date, il: bool) -> dict:
    today = _calendar(date, il)
    tomorrow = _calendar(date + dt.timedelta(days=1), il)
    out = {
        "shacharit": not _no_tachanun_day(today),
        "mincha": _mincha_tachanun(today, tomorrow),
    }
    if today.day_of_week == 7:
        # Tzidkatcha at Shabbat Mincha stands in for Tachanun: said when a
        # weekday Mincha on this date would say Tachanun (checked against
        # hebcal's tachanun(), whose Shabbat "mincha" means the same).
        out["tzidkatcha"] = _mincha_tachanun(today, tomorrow, count_shabbat=False)
    return out


def hallel(jc: JewishCalendar, il: bool) -> dict:
    m, d = jc.jewish_month, jc.jewish_day
    full = (
        jc.is_chanukah()
        or (m == NISAN and (d == 15 or (d == 16 and not il)))
        or (m == SIVAN and (d == 6 or (d == 7 and not il)))
        or (m == TISHREI and 15 <= d <= (22 if il else 23))
    )
    if full:
        return {"kind": "full", "beracha": True}
    if (m == NISAN and 16 <= d <= (21 if il else 22)) or jc.is_rosh_chodesh():
        return {"kind": "half", "beracha": False}
    return {"kind": "none", "beracha": False}


def _winter_gevurot(m: int, d: int) -> bool:
    """Mashiv HaRuach for the whole day (Shemini Atzeret and 15 Nisan are
    split by service in gevurot())."""
    if m == TISHREI:
        return d > 22
    if m == NISAN:
        return d < 15
    return m in (CHESHVAN, KISLEV, TEVET, SHEVAT, ADAR, ADAR_II)


def gevurot(jc: JewishCalendar) -> dict:
    m, d = jc.jewish_month, jc.jewish_day
    out = {}
    for service in SERVICES:
        if m == TISHREI and d == 22:
            winter = service in ("musaf", "mincha")
        elif m == NISAN and d == 15:
            winter = service in ("arbit", "shacharit")
        else:
            winter = _winter_gevurot(m, d)
        out[service] = "mashiv" if winter else "morid"
    return out


def _is_greg_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def diaspora_rain_start(year: int) -> dt.date:
    """The civil date whose Hebrew day's Arbit begins Barech Alenu outside
    Israel: that Arbit is the night of 4 December (5 December before a civil
    leap year), i.e. the Hebrew day of 5 (6) December."""
    return dt.date(year, 12, 6 if _is_greg_leap(year + 1) else 5)


def birkat_hashanim(date: dt.date, jc: JewishCalendar, il: bool) -> str:
    m, d = jc.jewish_month, jc.jewish_day
    summer = (m == NISAN and d >= 15) or m in (IYAR, SIVAN, TAMMUZ, AV, ELUL, TISHREI)
    if summer:
        return "barchenu"
    if il:
        return "barchenu" if (m == CHESHVAN and d < 7) else "barech-alenu"
    if date.month >= 9:
        return "barech-alenu" if date >= diaspora_rain_start(date.year) else "barchenu"
    return "barech-alenu"


def occasions(jc: JewishCalendar, il: bool) -> list[str]:
    """Keys matching the siddur's day tags (backend/siddur_lines.WHEN_KEYS),
    plus a few the Today card uses on its own."""
    m, d = jc.jewish_month, jc.jewish_day
    sd = jc.significant_day()
    active = []
    if jc.day_of_week == 7:
        active.append("shabbat")
        if m == TISHREI and 3 <= d <= 9:
            active.append("shabbat-shuva")
    if jc.is_rosh_chodesh():
        active.append("rosh-chodesh")
    if m == NISAN and 15 <= d <= (21 if il else 22):
        active.append("pesach")
        if 17 <= d <= 20 or (il and d == 16):
            active.append("chol-hamoed-pesach")
    if m == SIVAN and (d == 6 or (d == 7 and not il)):
        active.append("shavuot")
    if m == TISHREI:
        if d in (1, 2):
            active.append("rosh-hashana")
        if d <= 10:
            active.append("aseret-yemei-teshuva")
        if d == 10:
            active.append("yom-kippur")
        if 15 <= d <= 21:
            active.append("sukkot")
            if 17 <= d <= 21 or (il and d == 16):
                active.append("chol-hamoed-sukkot")
        if d == 22 or (d == 23 and not il):
            active.append("shemini-atzeret")
    if jc.is_chanukah():
        active.append("chanukah")
    if _is_purim_adar(jc) and d == 14:
        active.append("purim")
    if _is_purim_adar(jc) and d == 15:
        active.append("shushan-purim")
    if sd in _PUBLIC_FASTS:
        active.append("fast")
    return active


def _omer(date: dt.date, il: bool) -> int | None:
    return _calendar(date, il).day_of_omer()


def _hebrew(jc: JewishCalendar, date: dt.date) -> dict:
    hd = heb_dates.HebrewDate.from_pydate(date)
    return {
        "year": jc.jewish_year,
        "month": jc.jewish_month,
        "day": jc.jewish_day,
        "monthName": hd.month_name(),
        "he": hd.hebrew_date_string(),
    }


def day_guidance(date: dt.date, il: bool) -> dict:
    if not MIN_DATE <= date <= MAX_DATE:
        raise ValueError(f"date must be between {MIN_DATE} and {MAX_DATE}")
    jc = _calendar(date, il)
    active = occasions(jc, il)
    # Pesach/Sukkot here include their Chol HaMoed days.
    festival = any(k in active for k in ("pesach", "shavuot", "sukkot", "shemini-atzeret", "rosh-hashana", "yom-kippur"))
    return {
        "date": date.isoformat(),
        "il": il,
        "weekday": jc.day_of_week,  # 1 = Sunday ... 7 = Shabbat
        "hebrew": _hebrew(jc, date),
        "occasions": active,
        "tachanun": tachanun(date, il),
        "hallel": hallel(jc, il),
        "gevurot": gevurot(jc),
        "birkatHashanim": birkat_hashanim(date, jc, il),
        "yaalehVeyavo": "rosh-chodesh" in active or festival,
        "alHanissim": "chanukah" in active or "purim" in active,
        "alHanissimJerusalem": "shushan-purim" in active,
        "aseretYemeiTeshuva": "aseret-yemei-teshuva" in active,
        "musaf": "shabbat" in active or "rosh-chodesh" in active or festival,
        "aneinu": "fast" in active,
        "fastDay": _FAST_SLUGS.get(jc.significant_day()),
        "omer": {"today": _omer(date, il), "tonight": _omer(date + dt.timedelta(days=1), il)},
    }
