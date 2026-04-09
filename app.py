import json
import requests
from flask import Flask, render_template, request, jsonify, session, g
from dotenv import load_dotenv
import time
import os
from datetime import date as greg_date, timedelta, datetime
from functools import wraps
from urllib.parse import unquote

import jwt
try:
    from supabase import create_client
    try:
        from supabase.lib.client_options import SyncClientOptions
    except Exception:
        SyncClientOptions = None
except Exception:
    create_client = None
    SyncClientOptions = None

from pyluach import dates as pyluach_dates

from data_service import ShelahEngine
import sefaria
import claude

# Maps each prayer name to its constituent Sefaria "Siddur Sefard" refs for full text
SIDDUR_SECTION_MAP = {
    "Upon Arising": [
        "Siddur Sefard, Upon Arising, Modeh Ani",
        "Siddur Sefard, Upon Arising, Introductory Prayers",
        "Siddur Sefard, Upon Arising, Upon Entering Synagogue",
    ],
    "Weekday Shacharit": [
        "Siddur Sefard, Weekday Shacharit, Morning Blessings",
        "Siddur Sefard, Weekday Shacharit, Blessings on Torah",
        "Siddur Sefard, Weekday Shacharit, Morning Prayer",
        "Siddur Sefard, Weekday Shacharit, The Shema",
        "Siddur Sefard, Weekday Shacharit, Amidah",
        "Siddur Sefard, Weekday Shacharit, Tachanun",
        "Siddur Sefard, Weekday Shacharit, Aleinu",
    ],
    "Weekday Mincha": [
        "Siddur Sefard, Weekday Mincha, Amidah",
        "Siddur Sefard, Weekday Mincha, Tachanun",
    ],
    "Weekday Maariv": [
        "Siddur Sefard, Weekday Maariv, The Shema",
        "Siddur Sefard, Weekday Maariv, Amidah",
    ],
    "Shabbat Shacharit": [
        "Siddur Sefard, Shabbat Morning Services, Pesukei D'Zimrah",
        "Siddur Sefard, Shabbat Morning Services, Amidah",
        "Siddur Sefard, Shabbat Morning Services, Shabbat Torah Reading",
    ],
    "Shabbat Mincha": [
        "Siddur Sefard, Shabbat Mincha, Amidah",
    ],
    "Kiddush": [
        "Siddur Sefard, Shabbat Evening Meal, Shabbat Eve Kiddush",
        "Siddur Sefard, Shabbat Day Meal, Shabbat Day Kiddush",
    ],
    "Havdalah": [
        "Siddur Sefard, Motzaei Shabbat , Havdala",
    ],
    "Bedtime Shema": [
        "Siddur Sefard, Bedtime Shema",
    ],
    "Kiddush Levanah": [
        "Siddur Sefard, Kiddush Levanah",
    ],
    "Holiday Prayers": [
        "Siddur Sefard, Holidays, Yom Tov Eve Kiddush",
        "Siddur Sefard, Holidays, Yizkor",
        "Siddur Sefard, Rosh Chodesh, Hallel",
    ],
}

ANSWER_MODES = {"balanced", "practical", "sources"}

QUICK_TEXT_ALIASES = {
    "genesis": "Genesis 1",
    "bereishit": "Genesis 1",
    "exodus": "Exodus 1",
    "shemot": "Exodus 1",
    "leviticus": "Leviticus 1",
    "vayikra": "Leviticus 1",
    "numbers": "Numbers 1",
    "bamidbar": "Numbers 1",
    "deuteronomy": "Deuteronomy 1",
    "devarim": "Deuteronomy 1",
    "psalms": "Psalms 1",
    "tehillim": "Psalms 1",
    "proverbs": "Proverbs 1",
    "mishlei": "Proverbs 1",
}


def _sanitize_answer_mode(mode_value):
    mode = (mode_value or "balanced").strip().lower()
    return mode if mode in ANSWER_MODES else "balanced"


def _augment_question(original_question, mode, community_lens):
    guidance = {
        "balanced": "Give a concise answer first, then explain sources and differences in minhagim.",
        "practical": "Lead with practical next steps and real-life guidance before deeper analysis.",
        "sources": "Prioritize detailed source analysis and cite mekorot carefully before practical summary.",
    }

    parts = [
        f"User question: {original_question}",
        f"Answer style: {guidance.get(mode, guidance['balanced'])}",
    ]
    if community_lens and community_lens.lower() != "all":
        parts.append(f"Community lens requested: {community_lens}")
    return "\n".join(parts)


def _canonicalize_community_name(name):
    if not name:
        return None

    if name in COMMUNITIES:
        return name

    lowered = name.strip().lower()
    if lowered in COMMUNITY_ALIASES:
        return COMMUNITY_ALIASES[lowered]

    normalized = "".join(ch for ch in lowered if ch.isalnum())
    for alias, canonical in COMMUNITY_ALIASES.items():
        alias_norm = "".join(ch for ch in alias.lower() if ch.isalnum())
        if alias_norm == normalized:
            return canonical

    for canonical in COMMUNITIES.keys():
        canonical_norm = "".join(
            ch for ch in canonical.lower() if ch.isalnum())
        if canonical_norm == normalized:
            return canonical

    return None


def _detect_community_in_text(question):
    q_lower = (question or "").lower()
    for alias, canonical in sorted(COMMUNITY_ALIASES.items(), key=lambda x: len(x[0]), reverse=True):
        if alias in q_lower:
            return canonical

    for canonical in COMMUNITIES.keys():
        if canonical.lower() in q_lower:
            return canonical

    return None


def _build_pyluach_holiday_events(year):
    """Fallback holiday event list for FullCalendar when Hebcal is unavailable."""
    events = []
    try:
        current = greg_date(int(year), 1, 1)
        end = greg_date(int(year), 12, 31)
    except Exception:
        return events

    while current <= end:
        try:
            heb = pyluach_dates.GregorianDate(
                current.year, current.month, current.day).to_heb()
            holiday_name = heb.holiday()
            if holiday_name:
                events.append({
                    "title": f"🕎 {holiday_name}",
                    "start": current.isoformat(),
                    "allDay": True,
                    "display": "block",
                    "color": "#802f3e",
                    "textColor": "#ffffff",
                })
        except Exception:
            # Keep fallback generation resilient even if one date fails.
            pass
        current += timedelta(days=1)

    return events


load_dotenv()
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(32)

CLERK_PUBLISHABLE_KEY = (
    os.environ.get("CLERK_PUBLISHABLE_KEY")
    or os.environ.get("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY")
    or ""
).strip()
CLERK_JWT_ISSUER = (os.environ.get("CLERK_JWT_ISSUER")
                    or "").strip().rstrip("/")
CLERK_AUDIENCE = (os.environ.get("CLERK_AUDIENCE") or "").strip()
CLERK_ENFORCE_AUTH = (os.environ.get("CLERK_ENFORCE_AUTH")
                      or "false").strip().lower() == "true"
_clerk_jwks_client = None

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip()
if not SUPABASE_URL:
    SUPABASE_URL = (os.environ.get("NEXT_PUBLIC_SUPABASE_URL") or "").strip()

SUPABASE_PUBLISHABLE_KEY = (
    os.environ.get("SUPABASE_ANON_KEY")
    or os.environ.get("NEXT_PUBLIC_SUPABASE_PUBLISHABLE_DEFAULT_KEY")
    or os.environ.get("NEXT_PUBLIC_SUPABASE_ANON_KEY")
    or ""
).strip()

SUPABASE_SERVICE_ROLE_KEY = (os.environ.get(
    "SUPABASE_SERVICE_ROLE_KEY") or "").strip()
SUPABASE_PREFS_TABLE = (os.environ.get(
    "SUPABASE_PREFS_TABLE") or "user_preferences").strip()
_supabase_client = None


def _extract_bearer_token():
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header.split(" ", 1)[1].strip()
    return token or None


def _get_clerk_jwks_client():
    global _clerk_jwks_client
    if not CLERK_JWT_ISSUER:
        return None
    if _clerk_jwks_client is None:
        jwks_url = f"{CLERK_JWT_ISSUER}/.well-known/jwks.json"
        _clerk_jwks_client = jwt.PyJWKClient(jwks_url)
    return _clerk_jwks_client


def _verify_clerk_token(token):
    if not token:
        raise ValueError("Missing bearer token")
    if not CLERK_JWT_ISSUER:
        raise ValueError("Server missing CLERK_JWT_ISSUER")

    jwks_client = _get_clerk_jwks_client()
    if jwks_client is None:
        raise ValueError("Clerk JWKS client unavailable")

    signing_key = jwks_client.get_signing_key_from_jwt(token).key
    decode_kwargs = {
        "algorithms": ["RS256"],
        "issuer": CLERK_JWT_ISSUER,
    }
    if CLERK_AUDIENCE:
        decode_kwargs["audience"] = CLERK_AUDIENCE
    else:
        decode_kwargs["options"] = {"verify_aud": False}

    return jwt.decode(token, signing_key, **decode_kwargs)


def _get_supabase_client():
    global _supabase_client
    if create_client is None:
        return None
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    if _supabase_client is None:
        _supabase_client = create_client(
            SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    return _supabase_client


def _looks_like_jwt(value):
    if not isinstance(value, str):
        return False
    parts = value.split(".")
    return len(parts) == 3 and all(parts)


def _extract_supabase_token_from_cookie_value(raw_value):
    if not raw_value:
        return None

    decoded = unquote(raw_value)
    if _looks_like_jwt(decoded):
        return decoded

    try:
        parsed = json.loads(decoded)
    except Exception:
        return None

    if isinstance(parsed, dict):
        token = parsed.get("access_token") or parsed.get("accessToken")
        return token if isinstance(token, str) and token else None

    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, str) and _looks_like_jwt(item):
                return item
            if isinstance(item, dict):
                token = item.get("access_token") or item.get("accessToken")
                if isinstance(token, str) and token:
                    return token

    return None


def _extract_supabase_access_token():
    # Prefer Authorization header so API clients can override cookie auth.
    bearer = _extract_bearer_token()
    if bearer:
        return bearer

    direct_cookie_names = [
        "sb-access-token",
        "supabase-access-token",
    ]
    for cookie_name in direct_cookie_names:
        direct_value = request.cookies.get(cookie_name)
        token = _extract_supabase_token_from_cookie_value(direct_value)
        if token:
            return token

    session_cookie_values = []
    chunked_cookies = {}
    for cookie_name, cookie_value in request.cookies.items():
        if not (cookie_name.startswith("sb-") and "-auth-token" in cookie_name):
            continue

        if "." in cookie_name:
            base, suffix = cookie_name.rsplit(".", 1)
            if suffix.isdigit():
                chunked_cookies.setdefault(base, []).append(
                    (int(suffix), cookie_value))
                continue

        session_cookie_values.append(cookie_value)

    for cookie_value in session_cookie_values:
        token = _extract_supabase_token_from_cookie_value(cookie_value)
        if token:
            return token

    for _, chunk_parts in chunked_cookies.items():
        sorted_parts = sorted(chunk_parts, key=lambda part: part[0])
        joined_value = "".join(part[1] for part in sorted_parts)
        token = _extract_supabase_token_from_cookie_value(joined_value)
        if token:
            return token

    return None


def _get_request_supabase_client():
    """Flask equivalent of Next.js createServerClient for request-scoped reads."""
    if create_client is None:
        return None
    if not SUPABASE_URL or not SUPABASE_PUBLISHABLE_KEY:
        return None

    access_token = _extract_supabase_access_token()
    if not access_token or SyncClientOptions is None:
        return create_client(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY)

    auth_headers = {"Authorization": f"Bearer {access_token}"}
    try:
        options = SyncClientOptions(headers=auth_headers)
        return create_client(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, options=options)
    except TypeError:
        # Compatibility fallback for older supabase-py signatures.
        return create_client(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY)


def maybe_require_clerk_auth(route_fn):
    @wraps(route_fn)
    def wrapped(*args, **kwargs):
        token = _extract_bearer_token()
        if not token:
            if CLERK_ENFORCE_AUTH:
                return jsonify({"error": "Authentication required"}), 401
            return route_fn(*args, **kwargs)

        try:
            g.clerk_claims = _verify_clerk_token(token)
        except Exception:
            return jsonify({"error": "Invalid or expired Clerk token"}), 401

        return route_fn(*args, **kwargs)

    return wrapped


def require_clerk_auth(route_fn):
    @wraps(route_fn)
    def wrapped(*args, **kwargs):
        token = _extract_bearer_token()
        if not token:
            return jsonify({"error": "Authentication required"}), 401

        try:
            g.clerk_claims = _verify_clerk_token(token)
        except Exception:
            return jsonify({"error": "Invalid or expired Clerk token"}), 401

        return route_fn(*args, **kwargs)

    return wrapped


def _get_prayer_refs(prayer_name):
    """Resolve prayer/service name to a list of Sefaria refs."""
    resolved_name = (unquote(prayer_name or "") or "").strip()
    if resolved_name in SIDDUR_SECTION_MAP:
        return SIDDUR_SECTION_MAP[resolved_name]

    from sefaria_library import get_index_leaf_refs
    return get_index_leaf_refs(resolved_name, max_refs=80)


def _coerce_coordinate(value, min_value, max_value):
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric < min_value or numeric > max_value:
        return None
    return numeric


def _extract_client_ip():
    forwarded_for = (request.headers.get("X-Forwarded-For")
                     or "").split(",")[0].strip()
    if forwarded_for:
        return forwarded_for

    real_ip = (request.headers.get("X-Real-IP") or "").strip()
    if real_ip:
        return real_ip

    remote_ip = (request.remote_addr or "").strip()
    return remote_ip or None


def get_engine():
    # Instantiate engine using session location or IP fallback
    lat = _coerce_coordinate(session.get('lat'), -90, 90)
    lon = _coerce_coordinate(session.get('lon'), -180, 180)

    if lat is None or lon is None:
        client_ip = _extract_client_ip()
        ip_target = ""
        if client_ip and client_ip not in {"127.0.0.1", "::1"}:
            ip_target = client_ip

        try:
            # ip-api.com is free, no key required, ~45 req/min limit.
            # Use request IP from Vercel headers instead of server runtime IP.
            # ip-api free tier only supports HTTP; ipwho.is is HTTPS fallback.
            lookup_urls = [
                f"http://ip-api.com/json/{ip_target}?fields=status,lat,lon,timezone,query",
                f"https://ipwho.is/{ip_target}" if ip_target else "https://ipwho.is/",
            ]

            for lookup_url in lookup_urls:
                r = requests.get(lookup_url, timeout=3)
                data = r.json() if r.ok else {}

                ip_lat = None
                ip_lon = None
                if data.get("status") == "success":
                    ip_lat = _coerce_coordinate(data.get('lat'), -90, 90)
                    ip_lon = _coerce_coordinate(data.get('lon'), -180, 180)
                elif data.get("success") is True:
                    ip_lat = _coerce_coordinate(data.get('latitude'), -90, 90)
                    ip_lon = _coerce_coordinate(
                        data.get('longitude'), -180, 180)

                if ip_lat is not None and ip_lon is not None:
                    lat = ip_lat
                    lon = ip_lon
                    session['lat'] = lat
                    session['lon'] = lon
                    break
        except Exception:
            pass

    if lat is None or lon is None:
        lat, lon = (40.7128, -74.0060)

    return ShelahEngine(lat=lat, lon=lon)


@app.route("/")
def index():
    engine = get_engine()
    daily_study = engine.get_daily_learning()

    # We no longer need hebcal learning as per new architecture, relying on Sefaria cal
    return render_template(
        "index.html",
        daily=daily_study,
        clerk_publishable_key=CLERK_PUBLISHABLE_KEY,
        clerk_enforce_auth=CLERK_ENFORCE_AUTH,
    )


@app.route('/set_location', methods=['POST'])
def set_location():
    data = request.get_json(silent=True) or {}
    lat = _coerce_coordinate(data.get('lat'), -90, 90)
    lon = _coerce_coordinate(data.get('lon'), -180, 180)
    if lat is None or lon is None:
        return jsonify({"error": "Invalid coordinates"}), 400

    session['lat'] = lat
    session['lon'] = lon
    return jsonify({"status": "success", "lat": lat, "lon": lon})


@app.route('/api/zmanim')
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


@app.route('/api/zmanim/month')
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


@app.route("/ask", methods=["POST"])
@maybe_require_clerk_auth
def ask_question():
    data = request.json
    question = data.get("question", "")

    if not question:
        return jsonify({"error": "No question provided"}), 400
    mode = _sanitize_answer_mode(data.get("mode"))
    community_lens = (data.get("community") or "All").strip()
    canonical_lens = "All" if community_lens.lower() == "all" else (
        _canonicalize_community_name(community_lens) or community_lens)

    try:
        engine = get_engine()
        augmented_question = _augment_question(question, mode, community_lens)

        # Check for direct prayer-service questions
        if any(prayer in question for prayer in ["Shacharit", "Mincha", "Maariv", "Kiddush", "Havdalah"]):
            # Return a prayer service focused response
            return jsonify({
                "answer": f"Prayer Service Guide\n\n{question}\n\nYou can browse full liturgy books and services from the prayer sections. For practical application, compare local community custom with your rabbi's guidance.",
                "confidence": 0.85,
                "sources": [{
                    "ref": "Sefaria Liturgy",
                    "title": "Sefaria Prayer Books",
                    "lines": [{"en": f"Prayer Service: {question}", "he": ""}]
                }],
                "customs": [],
                "meta": {
                    "mode": mode,
                    "community_lens": canonical_lens,
                    "source_count": 1,
                    "custom_count": 0,
                    "generated_at": int(time.time()),
                    "fallback": False,
                }
            })

        # Check for Merkava/Community Customs requests
        detected_community = _detect_community_in_text(question)
        if detected_community or "customs" in question.lower() or "minhag" in question.lower():
            community = detected_community or (
                _canonicalize_community_name(community_lens) or "Ashkenaz")
            customs_query = question
            if canonical_lens != "All":
                customs_query = f"{question} {canonical_lens}"
            customs_info = engine.get_customs(customs_query)
            return jsonify({
                "answer": f"Community Customs ({community})\n\n{question}\n\nJewish communities from different diaspora regions developed distinct customs and practices while maintaining core halakhic principles. These traditions reflect the unique historical, cultural, and environmental contexts of each community.",
                "confidence": 0.8,
                "sources": [{
                    "ref": f"Merkava - {community} Customs",
                    "title": f"{community} Community Customs",
                    "lines": [{"en": f"Community: {community}", "he": ""}]
                }],
                "customs": customs_info,
                "meta": {
                    "mode": mode,
                    "community_lens": canonical_lens,
                    "source_count": 1,
                    "custom_count": len(customs_info),
                    "generated_at": int(time.time()),
                    "fallback": False,
                }
            })

        # 1. Fetch Sefaria Refs - Standard halakhic questions
        primary_refs = sefaria.find_refs_for_question(question)
        primary_sources = []
        for ref in primary_refs:
            # ref is a string, not a dict - get library text using the ref directly
            source_data = engine.get_library_text(ref)
            primary_sources.append(source_data)

        # 2. Fetch Halachipedia
        halachipedia_info = engine.get_halachipedia_summary(question)
        halachipedia_list = [halachipedia_info] if halachipedia_info else []

        # 3. Fetch Customs
        customs_query = question
        if canonical_lens != "All":
            customs_query = f"{question} {canonical_lens}"
        customs_info = engine.get_customs(customs_query)

        # 4. Fetch Wikipedia
        wiki_info = engine.get_wiki(question)
        wiki_list = [wiki_info] if wiki_info else []

        # 5. Build Claude Prompt
        # Passing primary_sources directly. We'll let claude.py format them.
        # We need to flat map the sources so Claude can easily read them in plain text, but the UI keeps the separated ones.
        flat_sources_for_claude = []
        for src in primary_sources:
            en_lines = [l['en'] for l in src['lines'] if l['en']]
            flat_sources_for_claude.append({
                'ref': src['ref'],
                'text': ' '.join(en_lines)
            })

        result = claude.get_halachic_answer(
            question=question,
            sefaria_sources=flat_sources_for_claude,
            customs=customs_info,
            wiki=wiki_list,
            halachipedia=halachipedia_list,
            mode=mode,
            community_lens=canonical_lens,
        )

        # Send all context back to the frontend
        return jsonify({
            "answer": result.get("answer"),
            "confidence": result.get("confidence"),
            "wiki": wiki_list + halachipedia_list,
            "customs": customs_info,
            "sources": primary_sources,
            "meta": {
                "mode": mode,
                "community_lens": canonical_lens,
                "source_count": len(primary_sources),
                "custom_count": len(customs_info),
                "generated_at": int(time.time()),
                "fallback": result.get("confidence", 1) < 0.5,
            }
        })

    except Exception as e:
        import traceback
        print(traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/stack/health")
def stack_health():
    """Return runtime readiness for Bento stack components."""
    supabase_ready = bool(_get_supabase_client())
    return jsonify({
        "flask": True,
        "vercel": True,
        "clerk": {
            "configured": bool(CLERK_PUBLISHABLE_KEY and CLERK_JWT_ISSUER),
            "enforced": CLERK_ENFORCE_AUTH,
        },
        "supabase": {
            "configured": bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY),
            "publishable_configured": bool(SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY),
            "ready": supabase_ready,
            "prefs_table": SUPABASE_PREFS_TABLE,
        },
        "calendar": {
            "pyluach": True,
            "zmanim": True,
        }
    })


@app.route("/api/auth/me")
def clerk_auth_me():
    """Returns Clerk auth status and a minimal user payload."""
    token = _extract_bearer_token()
    if not token:
        return jsonify({"authenticated": False})

    try:
        claims = _verify_clerk_token(token)
        return jsonify({
            "authenticated": True,
            "user_id": claims.get("sub"),
            "session_id": claims.get("sid"),
        })
    except Exception:
        return jsonify({"authenticated": False}), 401


@app.route("/api/user/preferences", methods=["GET", "PUT"])
@require_clerk_auth
def user_preferences():
    """Persist and fetch per-user UI preferences from Supabase."""
    claims = getattr(g, "clerk_claims", {}) or {}
    user_id = claims.get("sub")
    if not user_id:
        return jsonify({"error": "Missing user identity"}), 401

    supabase = _get_supabase_client()
    if not supabase:
        return jsonify({"error": "Supabase not configured"}), 503

    table = supabase.table(SUPABASE_PREFS_TABLE)

    try:
        if request.method == "GET":
            result = table.select("prefs,updated_at").eq(
                "user_id", user_id).limit(1).execute()
            rows = result.data or []
            if not rows:
                return jsonify({"prefs": None, "updated_at": None})

            record = rows[0]
            if not isinstance(record, dict):
                return jsonify({"prefs": None, "updated_at": None})

            return jsonify({
                "prefs": record.get("prefs"),
                "updated_at": record.get("updated_at"),
            })

        payload = request.get_json(silent=True) or {}
        prefs = payload.get("prefs")
        if not isinstance(prefs, dict):
            return jsonify({"error": "prefs must be an object"}), 400

        now_iso = datetime.utcnow().isoformat() + "Z"
        upsert_payload = {
            "user_id": user_id,
            "prefs": prefs,
            "updated_at": now_iso,
        }
        table.upsert(upsert_payload, on_conflict="user_id").execute()
        return jsonify({"ok": True, "updated_at": now_iso})
    except Exception as e:
        return jsonify({"error": f"Supabase operation failed: {str(e)}"}), 500


@app.route("/api/todos")
def list_todos():
    """Flask equivalent of the Next.js server query for todos."""
    supabase = _get_request_supabase_client()
    if not supabase:
        return jsonify({
            "error": "Supabase publishable client is not configured",
            "hint": "Set NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_PUBLISHABLE_DEFAULT_KEY",
        }), 503

    try:
        result = supabase.from_("todos").select("id,name").execute()
        return jsonify({"todos": result.data or []})
    except Exception as e:
        return jsonify({"error": f"Failed to load todos: {str(e)}"}), 500


@app.route("/api/library/index")
def library_index():
    """Returns the full Sefaria library category tree."""
    from sefaria_library import get_library_index
    data = get_library_index()
    return jsonify(data)


@app.route("/api/library/popular")
def library_popular():
    """Returns curated popular texts per category."""
    from sefaria_library import get_popular_texts
    return jsonify(get_popular_texts())


@app.route("/api/text/<path:ref>")
def get_text_inline(ref):
    """Fetches a Sefaria text inline — Hebrew + English + metadata."""
    from sefaria_library import get_text
    data = get_text(ref)
    return jsonify(data)


@app.route("/api/library/search")
def library_search():
    """Full-text search across all Sefaria texts."""
    from sefaria_library import search_library
    query = request.args.get("q", "")
    size = int(request.args.get("size", 10))
    if not query:
        return jsonify([])
    results = search_library(query, size=size)
    return jsonify(results)


@app.route("/api/search/suggest")
def search_suggest():
    """Omnibox suggestions: texts, prayers, communities, and AI query option."""
    from sefaria_library import search_library, get_liturgy_books

    query = (request.args.get("q", "") or "").strip()
    try:
        size = max(1, min(int(request.args.get("size", 8)), 20))
    except ValueError:
        size = 8
    if not query:
        return jsonify([])

    q_lower = query.lower()
    suggestions = []
    seen = set()

    def add_item(item_type, label, value, subtitle="", score=0, label_he="", subtitle_he=""):
        key = (item_type, (value or "").lower())
        if key in seen:
            return
        seen.add(key)
        suggestions.append({
            "type": item_type,
            "label": label,
            "label_he": label_he,
            "value": value,
            "subtitle": subtitle,
            "subtitle_he": subtitle_he,
            "score": score,
        })

    alias_ref = QUICK_TEXT_ALIASES.get(q_lower)
    if alias_ref:
        add_item("text", alias_ref, alias_ref, "Popular Torah alias", 100)

    for community in COMMUNITIES.keys():
        if q_lower in community.lower():
            add_item("community", community, community,
                     "Community customs", 90)

    for alias, canonical in COMMUNITY_ALIASES.items():
        if q_lower in alias and canonical in COMMUNITIES:
            add_item("community", canonical, canonical,
                     f"Community customs (matched '{alias}')", 88)

    for book in get_liturgy_books(max_items=120):
        title = book.get("title", "")
        if title and q_lower in title.lower():
            add_item("prayer", title, title, "Sefaria liturgy", 85)

    for hit in search_library(query, size=size):
        ref = hit.get("ref", "")
        he_ref = (hit.get("heRef", "") or "").strip()
        categories = " > ".join(hit.get("categories", [])[:3])
        if ref:
            add_item(
                "text",
                ref,
                ref,
                categories or "Sefaria text",
                70,
                label_he=he_ref or ref,
            )

    add_item("ask", f"Ask Sh'elah: {query}", query,
             "AI synthesis", 40)

    suggestions.sort(key=lambda x: x.get("score", 0), reverse=True)
    return jsonify(suggestions[:size])


@app.route("/api/text/<path:ref>/links")
def get_text_links(ref):
    """Returns all linked commentaries & parallel texts for a given ref."""
    from sefaria_library import get_linked_texts
    return jsonify(get_linked_texts(ref))


@app.route("/api/library/category/<path:category>")
def library_category(category):
    """Returns all books in a given Sefaria category."""
    from sefaria_library import get_category_contents
    return jsonify(get_category_contents(category))


# ─── PRAYER BOOK API (Siddur Sefard - Sefardic/Mediterranean Siddur) ──────────
# Prayer data is imported from siddur_sefard.py which contains the complete Sefardic
# prayer book downloaded from Sefaria and enhanced with traditional liturgical content.


@app.route("/api/prayers/list")
def get_prayers_list():
    """Returns all prayer books from Sefaria Liturgy plus legacy quick services."""
    from sefaria_library import get_liturgy_books

    items = []
    seen = set()

    for name in SIDDUR_SECTION_MAP.keys():
        items.append({"name": name, "title": name, "source": "legacy-service"})
        seen.add(name)

    for book in get_liturgy_books(max_items=200):
        title = book.get("title")
        if title and title not in seen:
            items.append({"name": title, "title": title,
                         "source": "sefaria-liturgy"})
            seen.add(title)

    return jsonify(items)


@app.route("/api/prayer/<name>")
def get_prayer(name):
    """Returns prayer-book preview content in English and Hebrew."""
    from sefaria_library import get_text

    resolved_name = (unquote(name or "") or "").strip()
    refs = _get_prayer_refs(resolved_name)
    if not refs:
        return jsonify({"error": f"Prayer '{resolved_name}' not found"}), 404

    preview = None
    for ref in refs[:12]:
        data = get_text(ref)
        if "error" not in data and (data.get("he") or data.get("en")):
            preview = data
            break

    if not preview:
        return jsonify({"error": f"Could not load prayer '{resolved_name}' from Sefaria"}), 404

    en_preview = "\n".join([l.get("en", "") for l in preview.get(
        "lines", []) if l.get("en")][:8]).strip()
    he_preview = "\n".join([l.get("he", "") for l in preview.get(
        "lines", []) if l.get("he")][:8]).strip()
    if not en_preview:
        en_preview = f"Preview available in Hebrew for {resolved_name}."
    if not he_preview:
        he_preview = f"תצוגה מקדימה זמינה באנגלית עבור {resolved_name}."

    prayer_data = {
        "en": en_preview,
        "he": he_preview,
    }

    return jsonify({
        "name": resolved_name,
        "title": resolved_name,
        "content": prayer_data,
        "languages": ["en", "he"]
    })


@app.route("/api/siddur/full/<path:prayer_name>")
def get_siddur_full(prayer_name):
    """Fetch full prayer text from Sefaria for any supported prayer service/book."""
    from sefaria_library import get_text

    resolved_name = (unquote(prayer_name or "") or "").strip()
    refs = _get_prayer_refs(resolved_name)
    if not refs:
        return jsonify({"error": f"No Sefaria mapping for '{resolved_name}'"}), 404

    combined_lines = []
    for ref in refs:
        data = get_text(ref)
        if "error" not in data and (data.get("he") or data.get("en")):
            section_title = ref.split(", ")[-1] if ", " in ref else ref
            he_title = data.get("heTitle", section_title)
            combined_lines.append({
                "he": f"<strong class='text-navy'>{he_title}</strong>",
                "en": f"<strong class='text-navy'>{section_title}</strong>",
                "type": "header"
            })
            combined_lines.extend(data.get("lines", []))

    if not combined_lines:
        return jsonify({"error": "Could not fetch prayer text from Sefaria"}), 404

    return jsonify({
        "prayer": resolved_name,
        "lines": combined_lines,
        "sources": refs
    })


# ─── COMMUNITY CUSTOMS API (Merkava) ──────────────────────────────────────────

COMMUNITIES = {
    "Ashkenaz": "ashkenaz",
    "Bukharian": "bukharian",
    "Ethiopian": "ethiopian",
    "Georgian": "georgian",
    "Greek-Romaniote": "greek-romaniote",
    "Iraqi": "iraqi",
    "Kavkazi": "mountain-jewish-kavkazi",
    "Syrian": "syrian",
    "Persian": "persian",
    "Sefardic": "sefardic",
    "Turkish-Ottoman Sefardic": "turkish-ottoman-sefardic",
    "Yemenite": "yemenite",
    "Moroccan": "moroccan",
    "Israeli": "sefardic",
    "Legacy Customs DB": "customs_db",
}

COMMUNITY_ALIASES = {
    "ashkenazi": "Ashkenaz",
    "ashkenaz": "Ashkenaz",
    "sefardi": "Sefardic",
    "sephardi": "Sefardic",
    "sefardic": "Sefardic",
    "sephardic": "Sefardic",
    "iraqi": "Iraqi",
    "mizrahi": "Iraqi",
    "syrian": "Syrian",
    "yemenite": "Yemenite",
    "yemeni": "Yemenite",
    "moroccan": "Moroccan",
    "morrocan": "Moroccan",
    "israeli": "Israeli",
    "israel": "Israeli",
    "kavkazi": "Kavkazi",
    "mountain jewish": "Kavkazi",
    "mountain-jewish": "Kavkazi",
    "kavkazi jews": "Kavkazi",
    "mountain-jewish-kavkazi": "Kavkazi",
    "bukharan": "Bukharian",
    "bukharian": "Bukharian",
    "ethiopian": "Ethiopian",
    "beta israel": "Ethiopian",
    "georgian": "Georgian",
    "persian": "Persian",
    "iranian": "Persian",
    "greek": "Greek-Romaniote",
    "romaniote": "Greek-Romaniote",
    "greek-romaniote": "Greek-Romaniote",
    "turkish": "Turkish-Ottoman Sefardic",
    "ottoman": "Turkish-Ottoman Sefardic",
    "ottoman sefardic": "Turkish-Ottoman Sefardic",
    "turkish ottoman": "Turkish-Ottoman Sefardic",
    "turkish-ottoman community": "Turkish-Ottoman Sefardic",
    "turkish ottoman community": "Turkish-Ottoman Sefardic",
    "turkish-ottoman": "Turkish-Ottoman Sefardic",
    "turkish-ottoman-sefardic": "Turkish-Ottoman Sefardic",
    "legacy customs db": "Legacy Customs DB",
    "customs_db": "Legacy Customs DB",
}


@app.route("/api/communities/list")
def get_communities_list():
    """Returns list of available communities."""
    communities = sorted(COMMUNITIES.keys())
    return jsonify([{"name": c} for c in communities])


@app.route("/api/community/<name>")
def get_community(name):
    """Returns community customs data."""
    canonical_name = _canonicalize_community_name(name)
    if canonical_name is None:
        return jsonify({"error": f"Community '{name}' not found"}), 404

    filename = COMMUNITIES[canonical_name]
    filepath = os.path.join(os.path.dirname(__file__),
                            "customs", f"{filename}.json")

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # Extract key information for display
        identity = data.get("identity", {})

        # Extract customs from halacha_index
        customs_content = {}
        for item in data.get("halacha_index", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            topic = item.get("topic", "").lower()
            category = item.get("category", "").lower()
            key = f"{category}_{topic}".strip("_")
            customs_content[key] = {
                "category": category,
                "topic": topic,
                "ruling": item.get("summary", ""),
                "common_practices": item.get("common_practices", []),
                "source": item.get("source", "")
            }

        fallback_customs = data if isinstance(data, dict) else {}

        return jsonify({
            "name": canonical_name,
            "requested_name": name,
            "heritage_id": data.get("heritage_id") if isinstance(data, dict) else None,
            "primary_origin": identity.get("primary_origin", "") if isinstance(identity, dict) else "",
            "customs": customs_content if customs_content else fallback_customs,
            "raw_data": data  # Full data available if needed
        })
    except Exception as e:
        return jsonify({"error": f"Could not load community data: {str(e)}"}), 500


# ─── TEXTS INDEX (for top menu) ───────────────────────────────────────────────
@app.route("/api/texts-index")
def get_texts_index():
    """Returns complete index of browsable texts: prayers, communities, Sefaria."""
    from sefaria_library import get_liturgy_books

    return jsonify({
        "siddur": {
            "title": "Sefaria Prayer Books",
            "items": [b.get("title") for b in get_liturgy_books(max_items=200)]
        },
        "merkava": {
            "title": "Community Customs (Merkava)",
            "items": list(COMMUNITIES.keys())
        },
        "sefaria": {
            "title": "Sefaria Library",
            "items": ["Tanakh", "Mishnah", "Talmud", "Halakhah", "Kabbalah"]
        }
    })


@app.route("/api/holidays")
def get_holidays():
    """Returns Jewish holiday events for FullCalendar via Hebcal API."""
    year = request.args.get('year', str(greg_date.today().year))
    url = (
        f"https://www.hebcal.com/hebcal?v=1&cfg=fc&maj=on&min=on&mod=on"
        f"&nx=on&year={year}&month=x&ss=on&mf=on&c=off&geo=none"
    )
    try:
        r = requests.get(url, timeout=5)
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            raise ValueError(data.get("error"))
        if isinstance(data, dict):
            return jsonify(data.get("items", []))
        if isinstance(data, list):
            return jsonify(data)
    except Exception:
        fallback = _build_pyluach_holiday_events(year)
        if fallback:
            return jsonify(fallback)

        # Last-resort fallback to monthly zmanim events so calendar is never empty.
        try:
            engine = get_engine()
            return jsonify(engine.get_monthly_zmanim())
        except Exception:
            return jsonify([])

    return jsonify([])


@app.route("/api/parasha")
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
        from calendar_service import calendar_engine
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


if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5001))
    app.run(debug=True, host='0.0.0.0', port=port)
