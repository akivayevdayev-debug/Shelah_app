"""
Calendar blueprint for Sh'elah.

Holiday, parasha, zmanim, location, and daily-study routing endpoints extracted
verbatim from ``app.py`` (Stage 2 blueprint split). Logic is unchanged; only the
route decorator target moved from ``@app.route`` to ``@routes_calendar.route``
and shared helpers/constants are imported from ``app`` and ``backend``.
"""

from datetime import date as greg_date

import requests
from flask import Blueprint, jsonify, request, session

from backend.data_service import ShelahEngine

from app import (
    app,
    get_engine,
    _coerce_coordinate,
    _strip_leading_symbol_prefix,
    _holiday_emoji_for_event,
    _holiday_color_for_category,
    _build_pyluach_holiday_events,
)

routes_calendar = Blueprint("calendar", __name__)


@routes_calendar.route('/set_location', methods=['POST'])
def set_location():
    data = request.get_json(silent=True) or {}
    lat = _coerce_coordinate(data.get('lat'), -90, 90)
    lon = _coerce_coordinate(data.get('lon'), -180, 180)
    if lat is None or lon is None:
        return jsonify({"error": "Invalid coordinates provided. Values must be numeric and within valid lat/lon ranges."}), 400

    session['lat'] = lat
    session['lon'] = lon
    return jsonify({"status": "success", "lat": lat, "lon": lon})


@routes_calendar.route('/api/zmanim')
def get_zmanim_api():
    community = request.args.get('community', 'standard')
    lat = _coerce_coordinate(request.args.get('lat'), -90, 90)
    lon = _coerce_coordinate(request.args.get('lon'), -180, 180)

    if lat is not None and lon is not None:
        session['lat'] = lat
        session['lon'] = lon
        engine = ShelahEngine(lat=lat, lon=lon)
    else:
        engine = get_engine()

    times = engine.get_zmanim(community)
    return jsonify(times)


@routes_calendar.route('/api/zmanim/month')
def get_zmanim_month():
    lat = _coerce_coordinate(request.args.get('lat'), -90, 90)
    lon = _coerce_coordinate(request.args.get('lon'), -180, 180)

    if lat is not None and lon is not None:
        session['lat'] = lat
        session['lon'] = lon
        engine = ShelahEngine(lat=lat, lon=lon)
    else:
        engine = get_engine()

    events = engine.get_monthly_zmanim()
    return jsonify(events)


@routes_calendar.route("/api/daily-study")
def daily_study_api():
    """Return daily refs for Daf Yomi, Rambam, and related daily study prewarming."""
    engine = get_engine()
    payload = engine.get_daily_learning() or {}
    return jsonify(payload)


@routes_calendar.route("/api/holidays")
def get_holidays():
    """Returns Jewish holiday events for FullCalendar via Hebcal API."""
    year = request.args.get('year', str(greg_date.today().year))
    url = (
        f"https://www.hebcal.com/hebcal?v=1&cfg=json&maj=on&min=on&mod=on"
        f"&nx=on&year={year}&month=x&ss=on&s=on&mf=on&c=off&geo=none"
    )
    try:
        r = requests.get(url, timeout=5)
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            raise ValueError(data.get("error"))
        items = data.get("items", []) if isinstance(data, dict) else data
        if not isinstance(items, list):
            items = []

        events = []
        for item in items:
            if not isinstance(item, dict):
                continue

            category = str(item.get("category") or "").strip().lower()
            title_raw = item.get("title") or ""
            title_clean = _strip_leading_symbol_prefix(title_raw)
            start = item.get("date") or item.get("start")

            if not title_clean or not start:
                continue

            emoji = _holiday_emoji_for_event(title_clean, category)
            events.append({
                "title": f"{emoji} {title_clean}",
                "start": start,
                "allDay": "T" not in str(start),
                "display": "block",
                "color": _holiday_color_for_category(category),
                "textColor": "#ffffff",
            })

        return jsonify(events)
    except Exception as e:
        app.logger.warning(f"Hebcal API failed for year {year}: {str(e)}")
        fallback = _build_pyluach_holiday_events(year) or []
        if fallback:
            return jsonify(fallback)

        # Last-resort fallback to monthly zmanim events so calendar is never empty.
        try:
            engine = get_engine()
            return jsonify(engine.get_monthly_zmanim())
        except Exception:
            return jsonify({"error": "Calendar data currently unavailable", "events": []}), 503


@routes_calendar.route("/api/parasha")
def get_parasha():
    """Return current weekly Parasha information for the Torah section."""
    try:
        r = requests.get("https://www.sefaria.org/api/calendars", timeout=6)
        data = r.json()

        for item in data.get("calendar_items", []):
            title_en = (item.get("title", {}) or {}).get("en", "")
            if "Parashat" in title_en or "Parasha" in title_en:
                display_en = (item.get("displayValue", {}) or {}).get("en", "")
                display_he = (item.get("displayValue", {}) or {}).get("he", "")
                ref = item.get("ref") or ""
                return jsonify({
                    "title": display_en or title_en,
                    "heTitle": display_he,
                    "ref": ref,
                    "source": "sefaria-calendars",
                })
    except Exception:
        pass

    try:
        from backend.calendar_service import calendar_engine
        parasha_name = calendar_engine.get_parasha()
        return jsonify({
            "title": parasha_name or "Parashat HaShavua",
            "heTitle": "",
            "ref": "Genesis 1",
            "source": "calendar-fallback",
        })
    except Exception:
        return jsonify({
            "title": "Parashat HaShavua",
            "heTitle": "",
            "ref": "Genesis 1",
            "source": "default-fallback",
        })
