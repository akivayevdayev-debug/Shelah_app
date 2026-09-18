"""
Calendar blueprint for Sh'elah.

Holiday, parasha, zmanim, location, and daily-study routing endpoints extracted
verbatim from ``app.py`` (Stage 2 blueprint split). Logic is unchanged; only the
route decorator target moved from ``@app.route`` to ``@routes_calendar.route``
and shared helpers/constants are imported from ``app`` and ``backend``.
"""

from datetime import date as greg_date

import requests
from flask import Blueprint, g, jsonify, request, session

from backend import sefaria
from backend.data_service import ShelahEngine
from backend.health_check import health
from backend.helpers import _is_same_origin_request, _coerce_int

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
    # Defense-in-depth (security audit P3): SESSION_COOKIE_SAMESITE=Lax
    # already blocks cross-site POST from carrying the session cookie, so
    # this isn't exploitable today, but the same origin check used
    # elsewhere in the app (routes_devtools.py's /api/client-errors) costs
    # nothing to add here too.
    if not _is_same_origin_request():
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json(silent=True) or {}
    lat = _coerce_coordinate(data.get('lat'), -90, 90)
    lon = _coerce_coordinate(data.get('lon'), -180, 180)
    if lat is None or lon is None:
        return jsonify({"error": "Invalid coordinates provided. Values must be numeric and within valid lat/lon ranges."}), 400

    session.permanent = True
    session['lat'] = lat
    session['lon'] = lon
    return jsonify({"status": "success", "lat": lat, "lon": lon})


def _fetch_geocode_results(query):
    """Fetch + parse Nominatim results for `query`.

    Returns (results, None) on success, or (None, error_response) on
    failure, where error_response is a ready-to-return (body, status) tuple.
    Split out of geocode_city() to keep the circuit-breaker/HTTP/parsing
    branches (SonarCloud python:S3776) out of the route handler's own
    complexity count.
    """
    if not health.is_healthy('nominatim'):
        return None, (jsonify({"error": "City search is temporarily unavailable."}), 503)

    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json",
                    "limit": 1, "addressdetails": 0},
            headers={
                "User-Agent": "ShelahApp/1.0 (halachic study app; contact via app support)",
                "Accept-Language": "en",
            },
            timeout=6,
        )
        if not r.ok:
            health.record_failure('nominatim')
            return None, (jsonify({"error": "City search failed."}), 502)
        results = r.json()
        health.record_success('nominatim')
        return results, None
    except (requests.RequestException, TimeoutError, ValueError) as exc:
        health.record_failure('nominatim')
        app.logger.warning("Geocode lookup failed for %r: %s", query, exc)
        return None, (jsonify({"error": "City search failed."}), 502)


@routes_calendar.route("/api/geocode")
def geocode_city():
    """Server-side proxy for the zmanim "search by city" box.

    The browser can't call Nominatim directly: our CSP connect-src doesn't
    (and shouldn't, for an ad-hoc third-party geocoder) allowlist it, and
    Nominatim's usage policy requires a real identifying User-Agent, which
    fetch() cannot set from the page. Proxying server-side fixes both and
    lets us circuit-break the dependency like every other external call.
    """
    query = (request.args.get('q') or '').strip()
    if not query:
        return jsonify({"error": "Missing query parameter 'q'."}), 400

    results, error_response = _fetch_geocode_results(query)
    if error_response is not None:
        return error_response

    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        return jsonify({"results": []})

    top = results[0]
    try:
        lat = float(top.get("lat"))
        lon = float(top.get("lon"))
    except (TypeError, ValueError):
        return jsonify({"results": []})

    return jsonify({
        "results": [{
            "lat": lat,
            "lon": lon,
            "display_name": str(top.get("display_name") or query),
        }]
    })


def _remember_location_if_same_origin(lat, lon):
    """Persist lat/lon into the session, but only for same-origin callers.

    Security audit P2: these are GET routes, and SESSION_COOKIE_SAMESITE=Lax
    explicitly *allows* cookies on cross-site top-level GET navigation — a
    crafted link could otherwise silently overwrite a victim's stored
    location via this side effect. Computing zmanim for the requested
    lat/lon is harmless regardless of origin (see callers below); only the
    session write needs this guard.
    """
    if _is_same_origin_request():
        session.permanent = True
        session['lat'] = lat
        session['lon'] = lon


def _resolve_zmanim_query_coords():
    """Parse+validate `lat`/`lon` query params for the GET zmanim routes.

    Returns (lat, lon, error_response). A param that's simply absent falls
    back to the default engine location (unchanged behavior). A param
    that's present but fails `_coerce_coordinate` (non-numeric or out of
    the valid lat/lon range) is a real client error, not a "no location
    given" case, and must not be silently swallowed into the same
    default-location fallback.
    """
    raw_lat = request.args.get('lat')
    raw_lon = request.args.get('lon')
    lat = _coerce_coordinate(raw_lat, -90, 90)
    lon = _coerce_coordinate(raw_lon, -180, 180)
    if (raw_lat is not None and lat is None) or (raw_lon is not None and lon is None):
        error_response = (jsonify({
            "error": "Invalid coordinates provided. Values must be numeric and within valid lat/lon ranges."
        }), 400)
        return None, None, error_response
    return lat, lon, None


@routes_calendar.route('/api/zmanim')
def get_zmanim_api():
    community = request.args.get('community', 'standard')
    lat, lon, error_response = _resolve_zmanim_query_coords()
    if error_response is not None:
        return error_response

    if lat is not None and lon is not None:
        _remember_location_if_same_origin(lat, lon)
        engine = ShelahEngine(lat=lat, lon=lon)
    else:
        # plan.md §14.3.1: no lat/lon in the URL -- get_engine() falls back to
        # session['lat']/session['lon'], so this response is NOT a pure
        # function of the URL and must never be cached publicly (one user's
        # location would leak to the next request that hits the CDN cache).
        g.cache_tier_force_private = True
        engine = get_engine()

    times = engine.get_zmanim(community)
    return jsonify(times)


@routes_calendar.route('/api/zmanim/month')
def get_zmanim_month():
    lat, lon, error_response = _resolve_zmanim_query_coords()
    if error_response is not None:
        return error_response

    if lat is not None and lon is not None:
        _remember_location_if_same_origin(lat, lon)
        engine = ShelahEngine(lat=lat, lon=lon)
    else:
        # See matching comment in get_zmanim_api() above.
        g.cache_tier_force_private = True
        engine = get_engine()

    events = engine.get_monthly_zmanim()
    return jsonify(events)


@routes_calendar.route("/api/daily-study")
def daily_study_api():
    """Return daily refs for Daf Yomi, Rambam, and related daily study prewarming.

    plan.md §47: this used to go through get_engine(), but
    ShelahEngine.get_daily_learning() forwards straight to
    sefaria.get_daily_study(), which never reads lat/lon (Hebcal is called
    with a hardcoded zip). get_engine() itself unconditionally touches
    Flask's session (session.get('lat')/session.get('lon')), which marks
    the session "accessed" and stamps Vary: Cookie on the response --
    defeating this route's CACHE_TIER_DATED CDN caching for no benefit.
    Calling sefaria directly keeps the response session-free.
    """
    payload = sefaria.get_daily_study() or {}
    return jsonify(payload)


def _holidays_fallback_chain(year, reason):
    """Pyluach -> monthly-zmanim -> 503 fallback ladder for /api/holidays.

    Shared by both the circuit-open early-exit and the request-exception
    except branch in get_holidays() below, so the fallback ladder itself
    (unchanged from before circuit-breaker wiring) lives in one place.
    """
    app.logger.warning(f"Hebcal API unavailable for year {year}: {reason}")
    fallback = _build_pyluach_holiday_events(year) or []
    if fallback:
        return jsonify(fallback)

    # Last-resort fallback to monthly zmanim events so calendar is never empty.
    try:
        # Same session-fallback caveat as get_zmanim_api()/get_zmanim_month().
        g.cache_tier_force_private = True
        engine = get_engine()
        return jsonify(engine.get_monthly_zmanim())
    except Exception:
        return jsonify({"error": "Calendar data currently unavailable", "events": []}), 503


@routes_calendar.route("/api/holidays")
def get_holidays():
    """Returns Jewish holiday events for FullCalendar via Hebcal API."""
    # plan.md §8.C.5 security-audit pass: `year` used to be interpolated into
    # the outbound Hebcal URL unvalidated, letting a value like
    # "2026&geo=pos&latitude=1" inject extra query parameters onto the real
    # request. Coerced to a bounded int first, matching every other numeric
    # query param in this codebase's _coerce_int convention.
    year = _coerce_int(
        request.args.get('year'), greg_date.today().year,
        min_value=1583, max_value=3000)
    url = (
        f"https://www.hebcal.com/hebcal?v=1&cfg=json&maj=on&min=on&mod=on"
        f"&nx=on&year={year}&month=x&ss=on&s=on&mf=on&c=off&geo=none"
    )

    if not health.is_healthy('hebcal'):
        return _holidays_fallback_chain(year, "circuit open")

    try:
        r = requests.get(url, timeout=5)
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            raise ValueError(data.get("error"))
        health.record_success('hebcal')
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
                "category": category or "default",
                "color": _holiday_color_for_category(category),
                "textColor": "#ffffff",
            })

        return jsonify(events)
    except Exception as e:
        health.record_failure('hebcal')
        return _holidays_fallback_chain(year, str(e))


@routes_calendar.route("/api/parasha")
def get_parasha():
    """Return current weekly Parasha information for the Torah section."""
    try:
        from backend.sefaria_library import _cached_get

        # The parasha only changes weekly; this endpoint previously called
        # Sefaria's calendars API on every single request with no caching
        # at all (~1.8s per hit, the single slowest hot-path endpoint on
        # the homepage). Route through the same memory+disk TTLCache every
        # other Sefaria fetch in this codebase already uses.
        data = _cached_get("https://www.sefaria.org/api/calendars", ttl=3600) or {}

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
