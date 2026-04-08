import pytz
from datetime import date, datetime, timedelta
from pyluach import dates as heb_dates
from zmanim.zmanim_calendar import ZmanimCalendar
from zmanim.util.geo_location import GeoLocation
from timezonefinder import TimezoneFinder
import requests
from calendar_service import calendar_engine

tf = TimezoneFinder()


def _resolve_timezone(lat, lon, given_tz=None):
    """Resolve a reliable pytz timezone object from coordinates."""
    tz_str = given_tz
    if not tz_str:
        try:
            tz_str = tf.timezone_at(lng=float(lon), lat=float(lat))
        except Exception:
            pass
    if not tz_str:
        tz_str = "America/New_York"
    return pytz.timezone(tz_str), tz_str


def _get_today_candle_lighting(lat, lon, timezone_str, current_date):
    """Fetch today's candle lighting time from Hebcal when available."""
    try:
        year = current_date.year
        month = current_date.month
        hebcal_url = (
            "https://www.hebcal.com/hebcal?v=1&cfg=json"
            f"&c=on&geo=pos&latitude={lat}&longitude={lon}"
            f"&tzid={timezone_str}&year={year}&month={month}&numMonths=1"
        )
        r = requests.get(hebcal_url, timeout=6)
        data = r.json()
        iso_day = current_date.isoformat()

        for item in data.get("items", []):
            if item.get("category") != "candles":
                continue
            stamp = item.get("date", "")
            if not stamp.startswith(iso_day):
                continue
            return datetime.fromisoformat(stamp)
    except Exception:
        return None

    return None


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

        holiday_info = calendar_engine.is_holiday()
        omer_info = _get_omer_info(today)

        # 2. Calculate requested key Halachic times
        dawn_16_1 = calendar.alos({'degrees': 16.1})
        talit_tefillin_10_2 = calendar.sunrise_offset_by_degrees(100.2)
        sunrise = calendar.sunrise()

        shema_gra = calendar.sof_zman_shma_gra()
        sunset_for_day = calendar.sunset()
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
        latest_musaf = None
        if holiday_info.get('is_holiday') and sunrise and sunset:
            shaah_zmanit = (sunset - sunrise) / 12
            latest_musaf = sunrise + (shaah_zmanit * 7)

        plag = calendar.plag_hamincha()

        candle_lighting = _get_today_candle_lighting(lat, lon, tz_name, today)
        if candle_lighting is None and (today.weekday() == 4 or holiday_info.get('is_holiday')):
            candle_lighting = calendar.candle_lighting()

        nightfall_3stars = calendar.tzais({'degrees': 8.5})

        next_alos_16_1 = next_day_calendar.alos({'degrees': 16.1})
        if sunset and next_alos_16_1 and next_alos_16_1 > sunset:
            midnight = sunset + ((next_alos_16_1 - sunset) / 2)
        else:
            midnight = chatzos + timedelta(hours=12) if chatzos else None

        # Custom Community Offsets
        is_friday = today.weekday() == 4
        shabbat_warning = ""

        now = datetime.now(tz)
        if is_friday and sunset:
            time_until_sunset = (sunset - now).total_seconds() / 60.0
            if 0 < time_until_sunset <= 18:
                shabbat_warning = "Shabbat is approaching! Less than 18 minutes to sunset."

        sunset_display = sunset
        if community.lower() == "bukharian" and sunset:
            sunset_display = sunset - timedelta(minutes=20)

        def fmt(t):
            return t.strftime('%I:%M %p') if t else "N/A"

        def fmt_iso(t):
            return t.isoformat() if t else None

        return {
            "metadata": {
                "date": today.strftime('%B %d, %Y'),
                "hebrew_date": calendar_engine.gregorian_to_hebrew()['hebrew_date'],
                "parasha": calendar_engine.get_parasha(),
                "holiday": holiday_info.get('holiday_name'),
                "is_holiday": bool(holiday_info.get('is_holiday')),
                "omer_day": omer_info.get('day') if omer_info else None,
                "omer_label": omer_info.get('label') if omer_info else "",
                "lat": lat,
                "lon": lon,
                "timezone": tz_name,
                "shabbat_warning": shabbat_warning,
                "zmanim_iso": {
                    "Dawn (16.1° / 72m)": fmt_iso(dawn_16_1),
                    "Earliest Tallit/Tefillin (10.2°)": fmt_iso(talit_tefillin_10_2),
                    "Sunrise": fmt_iso(sunrise),
                    "Latest Shema (GRA)": fmt_iso(shema_gra),
                    "Latest Shema (Baal HaTanya)": fmt_iso(shema_baal_hatanya),
                    "Latest Shacharit (GRA)": fmt_iso(tefilah_gra),
                    "Latest Shacharit (Baal HaTanya)": fmt_iso(tefilah_baal_hatanya),
                    "Chatzot (Midday)": fmt_iso(chatzos),
                    "Earliest Mincha (Mincha Gedola)": fmt_iso(mincha_gedola),
                    "Latest Musaf (Holidays)": fmt_iso(latest_musaf),
                    "Plag HaMincha": fmt_iso(plag),
                    "Candle Lighting (Holidays)": fmt_iso(candle_lighting),
                    "Sunset": fmt_iso(sunset_display),
                    "Nightfall (3 Stars)": fmt_iso(nightfall_3stars),
                    "Chatzot HaLailah (Midnight)": fmt_iso(midnight),
                }
            },
            "zmanim": {
                "Dawn (16.1° / 72m)": fmt(dawn_16_1),
                "Earliest Tallit/Tefillin (10.2°)": fmt(talit_tefillin_10_2),
                "Sunrise": fmt(sunrise),
                "Latest Shema (GRA)": fmt(shema_gra),
                "Latest Shema (Baal HaTanya)": fmt(shema_baal_hatanya),
                "Latest Shacharit (GRA)": fmt(tefilah_gra),
                "Latest Shacharit (Baal HaTanya)": fmt(tefilah_baal_hatanya),
                "Chatzot (Midday)": fmt(chatzos),
                "Earliest Mincha (Mincha Gedola)": fmt(mincha_gedola),
                "Latest Musaf (Holidays)": fmt(latest_musaf),
                "Plag HaMincha": fmt(plag),
                "Candle Lighting (Holidays)": fmt(candle_lighting),
                "Sunset": fmt(sunset_display) + (" (-20m)" if community.lower() == "bukharian" else ""),
                "Nightfall (3 Stars)": fmt(nightfall_3stars),
                "Chatzot HaLailah (Midnight)": fmt(midnight),
            }
        }
    except Exception as e:
        import traceback
        return {"error": str(e), "trace": traceback.format_exc()}


def get_monthly_events(lat, lon, timezone_str=None):
    """
    Generate FullCalendar events for the current month:
    - Daily sunrise, sunset & nightfall from KosherJava
    - Jewish holidays (with candle lighting times) from Hebcal API
    """
    import requests

    tz, tz_name = _resolve_timezone(lat, lon, timezone_str)
    location = GeoLocation("User Location", float(lat), float(lon), tz_name, 0)

    events = []
    today = date.today()

    # --- 1. Solar events for the next 30 days via KosherJava ---
    SOLAR_COLORS = {
        "Sunrise": "#B45309",        # amber-700
        "Sunset": "#1E3A5F",         # deep navy
        "Nightfall": "#4338CA",      # indigo
    }

    for i in range(30):
        current_date = today + timedelta(days=i)
        cal = ZmanimCalendar(geo_location=location, date=current_date)

        sunrise = cal.sunrise()
        sunset = cal.sunset()
        nightfall = cal.tzais({'degrees': 8.5})

        if sunrise:
            events.append({
                "title": f"🌅 Sunrise {sunrise.strftime('%I:%M %p')}",
                "start": sunrise.isoformat(),
                "color": SOLAR_COLORS["Sunrise"],
                "textColor": "#fff",
                "display": "block"
            })
        if sunset:
            events.append({
                "title": f"🌇 Shkia {sunset.strftime('%I:%M %p')}",
                "start": sunset.isoformat(),
                "color": SOLAR_COLORS["Sunset"],
                "textColor": "#fff",
                "display": "block"
            })
        if nightfall:
            events.append({
                "title": f"🌃 Nightfall {nightfall.strftime('%I:%M %p')}",
                "start": nightfall.isoformat(),
                "color": SOLAR_COLORS["Nightfall"],
                "textColor": "#fff",
                "display": "block"
            })

    # --- 2. Jewish Holidays from Hebcal ---
    HOLIDAY_COLORS = {
        "major": "#802f3e",          # Sefaria brick red
        "minor": "#594176",          # Sefaria purple
        "fast": "#374151",           # dark gray
        "shabbat": "#004e5f",        # Sefaria teal
        "roshchodesh": "#5a99b7",    # muted azure
        "candles": "#92400e",        # amber-brown
        "havdalah": "#374151",
    }

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
            f"&tzid={timezone_str}"
            f"&year={year}&month={month}&numMonths=2"
        )

        r = requests.get(hebcal_url, timeout=6)
        hdata = r.json()

        for item in hdata.get("items", []):
            category = item.get("category", "")
            title = item.get("title", "")
            date_str = item.get("date", "")  # ISO format

            # Pick color based on category
            color = HOLIDAY_COLORS.get(category, "#6B7280")

            # Build a richer title for candle lighting / havdalah
            emoji = ""
            if category == "candles":
                emoji = "🕯️ "
            elif category == "havdalah":
                emoji = "🌙 "
            elif category in ("major", "minor"):
                emoji = "✡️ "
            elif category == "fast":
                emoji = "⏳ "
            elif category == "roshchodesh":
                emoji = "🌙 "

            events.append({
                "title": f"{emoji}{title}",
                "start": date_str,
                "color": color,
                "textColor": "#fff",
                "allDay": "T" not in date_str  # all-day if no time component
            })

    except Exception as e:
        print(f"[Hebcal Error] {e}")

    return events
