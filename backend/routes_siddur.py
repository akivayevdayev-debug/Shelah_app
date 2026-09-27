"""Siddur v2 API (Prayers redesign, audit §2.4): the curated table of
contents, whole services of typed lines, and the day's prayer guidance.

Every response is a pure function of its URL -- the text is checked-in data
(backend/siddur_data.py) and the day guidance is computed from the query's
date and Israel flag alone (backend/siddur_day.py) -- so all three are
publicly cacheable (backend/cache_policy.py). The client passes the toc's
``version`` as ``?v=`` when it fetches a service, which the server ignores
but which gives each data version its own cache key.
"""

from __future__ import annotations

import datetime as dt

from flask import Blueprint, jsonify, request

from backend import siddur_data
from backend.siddur_day import MAX_DATE, MIN_DATE, day_guidance

routes_siddur = Blueprint("routes_siddur", __name__)


def _not_found(message: str):
    return jsonify({"error": message}), 404


@routes_siddur.route("/api/siddur/v2/toc/<rite>")
def siddur_toc(rite):
    toc = siddur_data.get_toc(rite)
    if toc is None:
        return _not_found(f"No siddur for rite '{rite}'")
    return jsonify(toc)


@routes_siddur.route("/api/siddur/v2/service/<rite>/<service>")
def siddur_service(rite, service):
    payload = siddur_data.get_service(rite, service)
    if payload is None:
        return _not_found(f"No service '{service}' in rite '{rite}'")
    return jsonify(payload)


@routes_siddur.route("/api/siddur/v2/day")
def siddur_day():
    raw_date = request.args.get("date", "")
    raw_il = request.args.get("il", "0")
    try:
        date = dt.date.fromisoformat(raw_date)
    except ValueError:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    if raw_il not in ("0", "1"):
        return jsonify({"error": "il must be 0 or 1"}), 400
    if not MIN_DATE <= date <= MAX_DATE:
        return jsonify({"error": f"date must be between {MIN_DATE} and {MAX_DATE}"}), 400
    return jsonify(day_guidance(date, raw_il == "1"))
