"""
Zmanim and calendar event engine.

Responsibilities:
- Resolve timezone from coordinates.
- Compute daily zmanim values via zmanim library.
- Enrich schedule with Hebcal candle-lighting and holiday context.
- Produce monthly event payloads for FullCalendar in the UI.

This module is the core time/calendar backend used by /api/zmanim and
/api/zmanim/month routes.
"""

import logging
from zoneinfo import ZoneInfo
from datetime import date, datetime, timedelta
from pyluach import dates as heb_dates
from zmanim.zmanim_calendar import ZmanimCalendar
from zmanim.util.geo_location import GeoLocation
from timezonefinder import TimezoneFinder
import requests
from backend.calendar_service import calendar_engine
from backend.cache import TTLCache
from backend.health_check import health

logger = logging.getLogger(__name__)

_tf = None
_HTTP = requests.Session()
_HEBCAL_DAY_CACHE_TTL_SECONDS = 60 * 30
_HEBCAL_MONTH_CACHE_TTL_SECONDS = 60 * 30
_HEBCAL_DAY_CACHE = TTLCache(ttl=_HEBCAL_DAY_CACHE_TTL_SECONDS)
_HEBCAL_MONTH_CACHE = TTLCache(ttl=_HEBCAL_MONTH_CACHE_TTL_SECONDS)
# One entry per (location, month): every candle-lighting/havdalah stamp Hebcal
# lists for that month, so a calendar card asking about a run of days costs
# one lookup instead of one per day.
_HEBCAL_CANDLES_MONTH_CACHE = TTLCache(ttl=_HEBCAL_DAY_CACHE_TTL_SECONDS)

# backend.calendar_service.PyluachEngine.is_holiday() names these fast days
# via pyluach's HebrewDate.holiday() -- exact strings pyluach returns, not
# Hebcal's (e.g. "Tzom Gedalia" not "Tzom Gedaliah", "9 of Av" not "Tisha
# B'Av"). Minor fasts run dawn-to-nightfall, entirely within their own civil
# day. Major fasts run sunset-to-nightfall -- like Shabbat/Yom Tov, the
# Hebrew day (and the fast) begins at the PRECEDING evening, so "today" only
# shows a fast-start time when TOMORROW is a major fast (see
# _compute_fast_times). Yom Kippur is a major fast AND a Yom Tov (it has
# Musaf, unlike every other fast day); it isn't listed as "minor" or
# repeated here since '_MAJOR_FAST_HOLIDAYS' membership alone drives its
# fast-start/-end handling below, independent of its Yom Tov status.
_MINOR_FAST_HOLIDAYS = frozenset({'Tzom Gedalia', '10 of Teves', 'Taanis Esther', '17 of Tamuz'})
_MAJOR_FAST_HOLIDAYS = frozenset({'Yom Kippur', '9 of Av'})


def _get_timezone_finder():
    """Lazy singleton: TimezoneFinder() loads a spatial boundary index on
    construction, so building it eagerly at import time bills every cold
    start even for requests that never resolve a timezone."""
    global _tf
    if _tf is None:
        _tf = TimezoneFinder()
    return _tf


def _cache_coord(value):
    try:
        return round(float(value), 4)
    except Exception:
        return value


def _resolve_timezone(lat, lon, given_tz=None):
    """Resolve a reliable stdlib zoneinfo timezone object from coordinates.

    Only ever used as datetime.now(tz) below (never .replace(tzinfo=...) or
    a bare tzinfo= constructor arg), so this is safe under both pytz and
    zoneinfo semantics -- no pytz.localize()-style normalization needed.
    """
    tz_str = given_tz
    if not tz_str:
        try:
            tz_str = _get_timezone_finder().timezone_at(lng=float(lon), lat=float(lat))
        except Exception:
            pass
    if not tz_str:
        tz_str = "America/New_York"
    return ZoneInfo(tz_str), tz_str


def _get_hebcal_day_times(lat, lon, timezone_str, current_date):
    """Fetch today's candle-lighting and havdalah timestamps from Hebcal when available."""
    cache_key = (
        _cache_coord(lat),
        _cache_coord(lon),
        str(timezone_str or ""),
        current_date.isoformat(),
    )
    cached = _HEBCAL_DAY_CACHE.get(cache_key)
    if isinstance(cached, dict):
        return dict(cached)

    result = {"candles": None, "havdalah": None}
    if not health.is_healthy('hebcal'):
        _HEBCAL_DAY_CACHE.set(cache_key, result)
        return dict(result)
    try:
        year = current_date.year
        month = current_date.month
        hebcal_url = (
            "https://www.hebcal.com/hebcal?v=1&cfg=json"
            # Without maj/min, Hebcal only emits the generic weekly Friday-
            # candle/Saturday-havdalah pair -- no holiday-specific candle
            # lighting (e.g. a Yom Tov's second-night lighting "from an
            # existing flame", which differs from the standard pre-sunset
            # offset) or holiday-ending havdalah gets returned at all.
            f"&maj=on&min=on"
            f"&c=on&geo=pos&latitude={lat}&longitude={lon}"
            f"&tzid={timezone_str}&year={year}&month={month}&numMonths=1"
        )
        r = _HTTP.get(hebcal_url, timeout=6)
        data = r.json()
        health.record_success('hebcal')
        iso_day = current_date.isoformat()

        for item in data.get("items", []):
            stamp = item.get("date", "")
            if not stamp.startswith(iso_day):
                continue
            category = item.get("category", "")
            if category == "candles":
                result["candles"] = datetime.fromisoformat(stamp)
            elif category == "havdalah":
                result["havdalah"] = datetime.fromisoformat(stamp)
    except Exception:
        health.record_failure('hebcal')
        _HEBCAL_DAY_CACHE.set(cache_key, result)
        return dict(result)

    _HEBCAL_DAY_CACHE.set(cache_key, result)
    return dict(result)


def _get_hebcal_month_candle_times(lat, lon, timezone_str, year, month):
    """Candle-lighting and havdalah stamps Hebcal lists for one Gregorian month.

    Returns {"YYYY-MM-DD": {"candles": datetime|None, "havdalah": datetime|None}};
    a day Hebcal doesn't mention (an ordinary weekday) is simply absent. Same
    query as _get_hebcal_day_times (holiday-aware, so a yom tov's own candle
    lighting and holiday-ending havdalah are included) but every day of the
    month is kept, not just one. A failure returns {} and is not cached, so
    the next request can retry once the health circuit allows it.
    """
    cache_key = (
        _cache_coord(lat), _cache_coord(lon), str(timezone_str or ""), year, month,
    )
    cached = _HEBCAL_CANDLES_MONTH_CACHE.get(cache_key)
    if isinstance(cached, dict):
        return {day: dict(times) for day, times in cached.items()}
    if not health.is_healthy('hebcal'):
        return {}

    hebcal_url = (
        "https://www.hebcal.com/hebcal?v=1&cfg=json&maj=on&min=on"
        f"&c=on&geo=pos&latitude={lat}&longitude={lon}"
        f"&tzid={timezone_str}&year={year}&month={month}&numMonths=1"
    )
    try:
        data = _HTTP.get(hebcal_url, timeout=6).json()
        health.record_success('hebcal')
    except Exception:
        health.record_failure('hebcal')
        return {}

    days = {}
    for item in data.get("items", []):
        category = item.get("category", "")
        stamp = item.get("date", "")
        if category not in ("candles", "havdalah") or len(stamp) < 10:
            continue
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        days.setdefault(stamp[:10], {"candles": None, "havdalah": None})[category] = when

    _HEBCAL_CANDLES_MONTH_CACHE.set(cache_key, days)
    return {day: dict(times) for day, times in days.items()}


def get_day_times(lat, lon, days, timezone_str=None):
    """Clock times for each requested date at a location.

    Feeds the calendar's holiday card, which shows "Begins: candle lighting
    6:24 PM" instead of just "Sundown". Every value is an ISO timestamp in the
    location's own timezone, or None when it doesn't exist that day (no
    candle lighting on a plain Tuesday) or can't be computed (polar summer).
    Candle lighting and havdalah come from Hebcal, like the main zmanim panel;
    dawn and nightfall use the same degrees as that panel.
    """
    _, tz_name = _resolve_timezone(lat, lon, timezone_str)
    location = GeoLocation("User Location", float(lat), float(lon), tz_name, 0)

    hebcal = {}
    for year, month in sorted({(d.year, d.month) for d in days}):
        hebcal.update(_get_hebcal_month_candle_times(lat, lon, tz_name, year, month))

    def iso(moment):
        return moment.isoformat() if moment else None

    result = {}
    for day in days:
        stamps = hebcal.get(day.isoformat(), {})
        entry = {
            "dawn": None, "sunset": None, "nightfall": None,
            "candles": iso(stamps.get("candles")),
            "havdalah": iso(stamps.get("havdalah")),
        }
        try:
            calendar = ZmanimCalendar(geo_location=location, date=day)
            entry["dawn"] = iso(calendar.alos({'degrees': 16.1}))
            entry["sunset"] = iso(calendar.sunset())
            entry["nightfall"] = iso(calendar.tzais({'degrees': 8.5}))
        except Exception:
            logger.warning("get_day_times: no solar times for %s at %s,%s", day, lat, lon)
        result[day.isoformat()] = entry
    return {"timezone": tz_name, "days": result}


def _get_weekly_shabbat_parasha(current_date):
    """Resolve this week's Shabbat parasha name (normalized without `Parashat` prefix)."""
    try:
        # Monday=0 ... Saturday=5 in Python's weekday numbering.
        days_until_shabbat = (5 - current_date.weekday()) % 7
        shabbat_date = current_date + timedelta(days=days_until_shabbat)
        raw = (calendar_engine.get_parasha(shabbat_date) or "").strip()
        if not raw:
            return ""

        lowered = raw.lower()
        if lowered.startswith("parashat "):
            return raw.split(" ", 1)[1].strip()
        if lowered.startswith("parasha "):
            return raw.split(" ", 1)[1].strip()
        return raw
    except Exception:
        return ""


def _get_omer_info(gregorian_day):
    """Return Omer day information (1-49) or None when out of season."""
    try:
        h = heb_dates.GregorianDate(
            gregorian_day.year,
            gregorian_day.month,
            gregorian_day.day,
        ).to_heb()
        omer_start = heb_dates.HebrewDate(h.year, 1, 16)  # 16 Nissan
        diff_days = h - omer_start
        if 0 <= diff_days <= 48:
            day_num = diff_days + 1
            return {
                "day": day_num,
                "label": f"Day {day_num} of 49",
            }
    except Exception:
        return None
    return None


def _compute_latest_musaf(is_yom_tov_today, is_shabbat, sunrise, sunset):
    """Musaf is only said on Shabbat, Rosh Chodesh, and true Yom Tov days
    (Rosh Hashana, Yom Kippur, Succos, Shmini Atzeres/Simchas Torah, Pesach,
    Shavuos) -- never on a plain fast day or a minor holiday like Chanukah
    or Purim. `is_yom_tov_today` must already reflect that narrower set
    (calendar_service.PyluachEngine.is_holiday()'s holiday_type == 'Yom
    Tov'), not "is any pyluach holiday", or this fires on every fast day
    too (the bug this docstring exists to prevent regressing back to)."""
    if (is_yom_tov_today or is_shabbat) and sunrise and sunset:
        shaah_zmanit = (sunset - sunrise) / 12
        return sunrise + (shaah_zmanit * 7)
    return None


def _compute_fast_times(today_holiday_name, tomorrow_holiday_name, dawn, nightfall,
                         hebcal_candle_lighting, calendar):
    """Fast-day start/end zmanim -- distinct from (and shown alongside)
    Candle Lighting/Havdalah, which only cover Shabbat/Yom Tov.

    Minor fasts (Tzom Gedalia, 10 Teves, Taanis Esther, 17 Tamuz) run
    dawn-to-nightfall entirely within their own civil day. Major fasts (Yom
    Kippur, 9 of Av) run sunset-to-nightfall: like any Hebrew day, the fast
    begins at the PRECEDING evening, so a fast-start time only shows when
    TOMORROW (not today) is tagged as the major fast -- "today" is Erev
    [fast], and it's tonight's own sunset that starts it, same reasoning as
    Friday's candle_lighting() starting Shabbat.
    """
    is_minor_fast_today = today_holiday_name in _MINOR_FAST_HOLIDAYS
    is_major_fast_today = today_holiday_name in _MAJOR_FAST_HOLIDAYS
    is_major_fast_starting_tonight = tomorrow_holiday_name in _MAJOR_FAST_HOLIDAYS

    fast_starts = None
    if is_minor_fast_today:
        fast_starts = dawn
    elif is_major_fast_starting_tonight:
        fast_starts = hebcal_candle_lighting if hebcal_candle_lighting is not None else calendar.candle_lighting()

    fast_ends = nightfall if (is_minor_fast_today or is_major_fast_today) else None
    return fast_starts, fast_ends


def _compute_candle_lighting(hebcal_candle_lighting, is_friday, is_yom_tov_tonight, calendar):
    """`is_yom_tov_tonight` must already be anchored on TOMORROW being a true
    Yom Tov day (see get_community_zmanim), matching how `is_friday` anchors
    on tomorrow being Shabbat -- candle lighting always happens the evening
    BEFORE the sacred day it starts, never on the sacred day's own morning."""
    show_candle_lighting = is_friday or is_yom_tov_tonight
    if not show_candle_lighting:
        return None
    if hebcal_candle_lighting is not None:
        return hebcal_candle_lighting
    return calendar.candle_lighting()


def _compute_havdalah(hebcal_havdalah, is_shabbat, is_holiday_last_day, nightfall_3stars):
    show_havdalah = is_shabbat or is_holiday_last_day
    if not show_havdalah:
        return None
    if hebcal_havdalah is not None:
        return hebcal_havdalah
    return nightfall_3stars


def _compute_midnight(sunset, next_alos_16_1, chatzos):
    if sunset and next_alos_16_1 and next_alos_16_1 > sunset:
        return sunset + ((next_alos_16_1 - sunset) / 2)
    return chatzos + timedelta(hours=12) if chatzos else None


def _compute_shabbat_warning(is_friday, sunset, now):
    if is_friday and sunset:
        time_until_sunset = (sunset - now).total_seconds() / 60.0
        if 0 < time_until_sunset <= 18:
            return "Shabbat is approaching! Less than 18 minutes to sunset."
    return ""


def _compute_sunset_display(sunset, community):
    if community.lower() == "bukharian" and sunset:
        return sunset - timedelta(minutes=20)
    return sunset


def get_community_zmanim(lat, lon, timezone_str=None, community="standard"):
    """
    Calculates expanded halachic times using the KosherJava port (zmanim library).
    Includes degree-based zmanim, shema/tefila variants, holiday-specific musaf/candle time,
    and midnight (chatzot halailah).
    """
    try:
        tz, tz_name = _resolve_timezone(lat, lon, timezone_str)
        # Use the target location timezone date, not server local date.
        today = datetime.now(tz).date()

        # 1. Setup Location & Calendar
        location = GeoLocation(
            "User Location", float(lat), float(lon), tz_name, 0)
        calendar = ZmanimCalendar(geo_location=location, date=today)
        next_day_calendar = ZmanimCalendar(
            geo_location=location,
            date=today + timedelta(days=1)
        )

        # 2. Calculate requested key Halachic times
        dawn_16_1 = calendar.alos({'degrees': 16.1})
        talit_tefillin_10_2 = calendar.sunrise_offset_by_degrees(100.2)
        sunrise = calendar.sunrise()

        shema_gra = calendar.sof_zman_shma_gra()
        sunset_for_day = calendar.sunset()

        # Jewish dates roll at sunset, not midnight.
        now = datetime.now(tz)
        halachic_date = today + \
            timedelta(
                days=1) if sunset_for_day and now >= sunset_for_day else today

        # Keep zmanim calculations aligned to civil day while metadata follows halachic day.
        civil_holiday_info = calendar_engine.is_holiday(today)
        tomorrow_holiday_info = calendar_engine.is_holiday(
            today + timedelta(days=1))
        holiday_info = calendar_engine.is_holiday(halachic_date)
        omer_info = _get_omer_info(halachic_date)
        hebrew_date_info = calendar_engine.gregorian_to_hebrew(halachic_date)
        parasha_name = calendar_engine.get_parasha(halachic_date)
        weekly_shabbat_parasha = _get_weekly_shabbat_parasha(today)

        shema_baal_hatanya = (
            calendar.sof_zman_shma(day_start=sunrise, day_end=sunset_for_day)
            if sunrise and sunset_for_day else None
        )

        tefilah_gra = calendar.sof_zman_tfila_gra()
        tefilah_baal_hatanya = (
            calendar.sof_zman_tfila(day_start=sunrise, day_end=sunset_for_day)
            if sunrise and sunset_for_day else None
        )

        chatzos = calendar.chatzos()
        mincha_gedola = calendar.mincha_gedola()
        sunset = sunset_for_day
        # "Yom Tov" (not "any pyluach holiday") gates Musaf/Candle Lighting/
        # Havdalah -- those must stay off on a plain fast day (Tzom Gedalia,
        # 9 of Av, ...) or a minor holiday (Chanukah, Purim, ...), none of
        # which have Musaf or a Shabbat/Yom-Tov-style candle lighting or
        # havdalah. `is_holiday`/`holiday_name` in the metadata below still
        # reflect ANY pyluach holiday (fasts included) -- that's a correct,
        # separate "what's special about today" signal, not a Musaf gate.
        is_yom_tov_today = civil_holiday_info.get('holiday_type') == 'Yom Tov'
        # Musaf is said every day of Sukkot/Pesach, Chol HaMoed included, so
        # `is_yom_tov_today` (above) is deliberately Chol-HaMoed-inclusive
        # for that purpose. Candle Lighting/Havdalah day-boundary detection
        # needs the OPPOSITE: it must exclude Chol HaMoed and Hoshana
        # Rabbah, or Shmini Atzeres's start-day would structurally never
        # fire (Hoshana Rabbah, tagged with the same bare 'Succos' name, would
        # always look like "yesterday was also Yom Tov" and cancel it out) --
        # calendar_service.is_holiday()'s `is_yom_tov_proper` already carries
        # that narrower, day-of-month-aware distinction.
        is_yom_tov_proper_today = bool(civil_holiday_info.get('is_yom_tov_proper'))
        is_yom_tov_proper_tomorrow = bool(tomorrow_holiday_info.get('is_yom_tov_proper'))
        # Candle lighting always happens the evening BEFORE the sacred day it
        # starts (like Friday's sunset starting Shabbat) -- so this must be
        # anchored on TOMORROW being a true Yom Tov day, not on today being
        # one. `is_yom_tov_proper_tomorrow` alone covers both real cases:
        # true Erev Yom Tov (today isn't Yom Tov, tomorrow is) AND a 2-day
        # Yom Tov's second night, lit from an existing flame (today AND
        # tomorrow are both Yom Tov). Anchoring on "today" instead (the
        # previous logic) only ever caught the second case, silently
        # skipping the first -- the actual evening most people are checking
        # this for.
        is_yom_tov_tonight = is_yom_tov_proper_tomorrow
        is_holiday_last_day = is_yom_tov_proper_today and not is_yom_tov_proper_tomorrow
        is_friday = today.weekday() == 4
        is_shabbat = today.weekday() == 5

        latest_musaf = _compute_latest_musaf(is_yom_tov_today, is_shabbat, sunrise, sunset)

        plag = calendar.plag_hamincha()

        hebcal_day_times = _get_hebcal_day_times(lat, lon, tz_name, today)
        candle_lighting = _compute_candle_lighting(
            hebcal_day_times.get("candles"), is_friday, is_yom_tov_tonight, calendar)

        nightfall_3stars = calendar.tzais({'degrees': 8.5})
        maariv_time = nightfall_3stars

        havdalah_time = _compute_havdalah(
            hebcal_day_times.get("havdalah"), is_shabbat, is_holiday_last_day, nightfall_3stars)

        fast_starts, fast_ends = _compute_fast_times(
            civil_holiday_info.get('holiday_name'),
            tomorrow_holiday_info.get('holiday_name'),
            dawn_16_1, nightfall_3stars,
            hebcal_day_times.get("candles"), calendar)

        next_alos_16_1 = next_day_calendar.alos({'degrees': 16.1})
        midnight = _compute_midnight(sunset, next_alos_16_1, chatzos)

        # Custom Community Offsets
        shabbat_warning = _compute_shabbat_warning(is_friday, sunset, now)
        sunset_display = _compute_sunset_display(sunset, community)

        def fmt(t):
            return t.strftime('%I:%M %p') if t else "N/A"

        def fmt_iso(t):
            return t.isoformat() if t else None

        return {
            "metadata": {
                "date": today.strftime('%B %d, %Y'),
                "hebrew_date": hebrew_date_info.get('hebrew_date', 'Unknown Date'),
                "parasha": parasha_name,
                "weekly_shabbat_parasha": weekly_shabbat_parasha,
                "holiday": holiday_info.get('holiday_name'),
                "is_holiday": bool(holiday_info.get('is_holiday')),
                "is_holiday_start_day": is_yom_tov_tonight,
                "is_holiday_last_day": is_holiday_last_day,
                "is_shabbat": is_shabbat,
                "omer_day": omer_info.get('day') if omer_info else None,
                "omer_label": omer_info.get('label') if omer_info else "",
                "lat": lat,
                "lon": lon,
                "timezone": tz_name,
                "shabbat_warning": shabbat_warning,
                "zmanim_iso": {
                    "Dawn (16.1° / 72m)": fmt_iso(dawn_16_1),
                    "Fast Starts": fmt_iso(fast_starts),
                    "Earliest Tallit/Tefillin (10.2°)": fmt_iso(talit_tefillin_10_2),
                    "Sunrise": fmt_iso(sunrise),
                    "Latest Shema (GRA)": fmt_iso(shema_gra),
                    "Latest Shema (Baal HaTanya)": fmt_iso(shema_baal_hatanya),
                    "Latest Shacharit (GRA)": fmt_iso(tefilah_gra),
                    "Latest Shacharit (Baal HaTanya)": fmt_iso(tefilah_baal_hatanya),
                    "Chatzot (Midday)": fmt_iso(chatzos),
                    "Earliest Mincha (Mincha Gedola)": fmt_iso(mincha_gedola),
                    "Latest Musaf": fmt_iso(latest_musaf),
                    "Plag HaMincha": fmt_iso(plag),
                    "Candle Lighting": fmt_iso(candle_lighting),
                    "Sunset": fmt_iso(sunset_display),
                    "Arvit (Maariv)": fmt_iso(maariv_time),
                    "Nightfall (3 Stars)": fmt_iso(nightfall_3stars),
                    "Fast Ends": fmt_iso(fast_ends),
                    "Havdalah": fmt_iso(havdalah_time),
                    "Chatzot HaLailah (Midnight)": fmt_iso(midnight),
                }
            },
            "zmanim": {
                "Dawn (16.1° / 72m)": fmt(dawn_16_1),
                "Fast Starts": fmt(fast_starts),
                "Earliest Tallit/Tefillin (10.2°)": fmt(talit_tefillin_10_2),
                "Sunrise": fmt(sunrise),
                "Latest Shema (GRA)": fmt(shema_gra),
                "Latest Shema (Baal HaTanya)": fmt(shema_baal_hatanya),
                "Latest Shacharit (GRA)": fmt(tefilah_gra),
                "Latest Shacharit (Baal HaTanya)": fmt(tefilah_baal_hatanya),
                "Chatzot (Midday)": fmt(chatzos),
                "Earliest Mincha (Mincha Gedola)": fmt(mincha_gedola),
                "Latest Musaf": fmt(latest_musaf),
                "Plag HaMincha": fmt(plag),
                "Candle Lighting": fmt(candle_lighting),
                "Sunset": fmt(sunset_display) + (" (-20m)" if community.lower() == "bukharian" else ""),
                "Arvit (Maariv)": fmt(maariv_time),
                "Nightfall (3 Stars)": fmt(nightfall_3stars),
                "Fast Ends": fmt(fast_ends),
                "Havdalah": fmt(havdalah_time),
                "Chatzot HaLailah (Midnight)": fmt(midnight),
            }
        }
    except Exception as e:
        logger.exception("get_community_zmanim failed: %s", e)
        return {"error": "Failed to compute zmanim for this location."}


_SOLAR_EVENT_COLORS = {
    "Sunrise": "#B45309",        # amber-700
    "Sunset": "#1E3A5F",         # deep navy
    "Nightfall": "#4338CA",      # indigo
}

_HEBCAL_HOLIDAY_COLORS = {
    "major": "#802f3e",          # Sefaria brick red
    "minor": "#594176",          # Sefaria purple
    "fast": "#374151",           # dark gray
    "shabbat": "#004e5f",        # Sefaria teal
    "roshchodesh": "#5a99b7",    # muted azure
    "candles": "#92400e",        # amber-brown
    "havdalah": "#374151",
}

_HEBCAL_CATEGORY_EMOJI = {
    "candles": "🕯️ ",
    "havdalah": "🌙 ",
    "major": "✡️ ",
    "minor": "✡️ ",
    "fast": "⏳ ",
    "roshchodesh": "🌙 ",
}


def _build_solar_day_events(cal):
    """One day's sunrise/sunset/nightfall FullCalendar events.

    Split out of get_monthly_events()'s per-day loop to keep the three
    presence checks out of that function's own complexity count
    (SonarCloud python:S3776).
    """
    events = []
    sunrise = cal.sunrise()
    sunset = cal.sunset()
    nightfall = cal.tzais({'degrees': 8.5})

    if sunrise:
        events.append({
            "title": f"🌅 Sunrise {sunrise.strftime('%I:%M %p')}",
            "start": sunrise.isoformat(),
            "color": _SOLAR_EVENT_COLORS["Sunrise"],
            "textColor": "#fff",
            "display": "block"
        })
    if sunset:
        events.append({
            "title": f"🌇 Shkia {sunset.strftime('%I:%M %p')}",
            "start": sunset.isoformat(),
            "color": _SOLAR_EVENT_COLORS["Sunset"],
            "textColor": "#fff",
            "display": "block"
        })
    if nightfall:
        events.append({
            "title": f"🌃 Nightfall {nightfall.strftime('%I:%M %p')}",
            "start": nightfall.isoformat(),
            "color": _SOLAR_EVENT_COLORS["Nightfall"],
            "textColor": "#fff",
            "display": "block"
        })
    return events


def _hebcal_event_from_item(item):
    """One Hebcal API item -> a FullCalendar event dict.

    Split out of get_monthly_events()'s Hebcal-items loop to keep the
    category/emoji dispatch out of that function's own complexity count
    (SonarCloud python:S3776).
    """
    category = item.get("category", "")
    title = item.get("title", "")
    date_str = item.get("date", "")  # ISO format
    return {
        "title": f"{_HEBCAL_CATEGORY_EMOJI.get(category, '')}{title}",
        "start": date_str,
        "color": _HEBCAL_HOLIDAY_COLORS.get(category, "#6B7280"),
        "textColor": "#fff",
        "allDay": "T" not in date_str  # all-day if no time component
    }


def get_monthly_events(lat, lon, timezone_str=None):
    """
    Generate FullCalendar events for the current month:
    - Daily sunrise, sunset & nightfall from KosherJava
    - Jewish holidays (with candle lighting times) from Hebcal API
    """
    _, tz_name = _resolve_timezone(lat, lon, timezone_str)
    location = GeoLocation("User Location", float(lat), float(lon), tz_name, 0)

    events = []
    today = date.today()
    month_cache_key = (
        _cache_coord(lat),
        _cache_coord(lon),
        str(tz_name or ""),
        today.year,
        today.month,
    )
    cached_events = _HEBCAL_MONTH_CACHE.get(month_cache_key)
    if isinstance(cached_events, list):
        return list(cached_events)

    # --- 1. Solar events for the next 30 days via KosherJava ---
    for i in range(30):
        current_date = today + timedelta(days=i)
        cal = ZmanimCalendar(geo_location=location, date=current_date)
        events.extend(_build_solar_day_events(cal))

    # --- 2. Jewish Holidays from Hebcal ---
    if health.is_healthy('hebcal'):
        try:
            # Fetch 2 months forward to ensure we cover the rest of the current month
            year = today.year
            month = today.month

            hebcal_url = (
                f"https://www.hebcal.com/hebcal?v=1&cfg=json"
                # major, minor, rosh chodesh, fast, shabbat, special shabbat
                f"&maj=on&min=on&nx=on&mf=on&ss=on&s=on"
                # candle lighting + user location
                f"&c=on&geo=pos&latitude={lat}&longitude={lon}"
                f"&tzid={tz_name}"
                f"&year={year}&month={month}&numMonths=2"
            )

            r = _HTTP.get(hebcal_url, timeout=6)
            hdata = r.json()
            health.record_success('hebcal')
            events.extend(_hebcal_event_from_item(item) for item in hdata.get("items", []))

        except Exception as e:
            health.record_failure('hebcal')
            logger.exception("[Hebcal Error] %s", e)

    _HEBCAL_MONTH_CACHE.set(month_cache_key, list(events))
    return events
