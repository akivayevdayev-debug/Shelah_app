"""
Calendar Service - Pyluach-first date orchestrator for Sh'elah
Primary source of truth for Hebrew/Gregorian conversions, holiday detection, and parasha lookups.
Validates against Hebcal API to ensure consistency across calendar systems.
"""

import logging
from pyluach import dates
import requests
from datetime import date as date_lib
import time

from backend.health_check import health

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
_HTTP = requests.Session()
_PARASHA_CACHE = {}
_PARASHA_CACHE_TTL_SECONDS = 60 * 60 * 6


class PyluachEngine:
    """Pyluach-first calendar engine with Hebcal validation."""

    @staticmethod
    def gregorian_to_hebrew(gregorian_date=None):
        """Convert Gregorian date to Hebrew date using Pyluach."""
        try:
            if gregorian_date is None:
                gregorian_date = date_lib.today()
            elif isinstance(gregorian_date, str):
                # Parse ISO format
                y, m, d = map(int, gregorian_date.split('-'))
                gregorian_date = date_lib(y, m, d)

            greg = dates.GregorianDate(
                gregorian_date.year,
                gregorian_date.month,
                gregorian_date.day
            )
            hebrew = greg.to_heb()

            # Get month name using month_name() method
            month_name = hebrew.month_name()
            hebrew_str = f"{hebrew.day} {month_name} {hebrew.year}"

            return {
                'hebrew_date': hebrew_str,
                'hebrew_year': hebrew.year,
                'hebrew_month': hebrew.month,
                'hebrew_day': hebrew.day,
                'gregorian_date': str(gregorian_date)
            }
        except Exception as e:
            logger.exception("Error converting %s to Hebrew", gregorian_date)
            return {'hebrew_date': 'Error', 'error': str(e)}

    @staticmethod
    def get_parasha(gregorian_date=None):
        """Get Torah portion for the week (from Hebcal API)."""
        try:
            if gregorian_date is None:
                gregorian_date = date_lib.today()
            elif isinstance(gregorian_date, str):
                y, m, d = map(int, gregorian_date.split('-'))
                gregorian_date = date_lib(y, m, d)

            cache_key = gregorian_date.isoformat()
            now = time.time()
            cached = _PARASHA_CACHE.get(cache_key)
            if cached and now - cached.get("ts", 0) < _PARASHA_CACHE_TTL_SECONDS:
                return cached.get("value")

            # Use Hebcal converter API for parasha information in events array.
            # Circuit-broken like every other hebcal call site (plan.md §8.E /
            # Prompt 17 item 1). The is_healthy gate + record_success/
            # record_failure wrap only the network call itself -- not the
            # date-parsing/cache-lookup above -- so a malformed
            # `gregorian_date` is never misattributed as a hebcal outage. A
            # circuit-open skip returns the same "Parasha lookup unavailable"
            # fallback directly (matching every other hebcal call site's
            # silent-skip behavior); a real request error is instead
            # re-raised to be caught by this method's own outer except below,
            # which logs it and returns that same fallback.
            if not health.is_healthy('hebcal'):
                return "Parasha lookup unavailable"
            try:
                response = _HTTP.get(
                    "https://www.hebcal.com/converter",
                    params={
                        "g2h": "on",
                        "gy": gregorian_date.year,
                        "gm": gregorian_date.month,
                        "gd": gregorian_date.day,
                        "cfg": "json",
                    },
                    timeout=5,
                )
                response.raise_for_status()
                data = response.json()
                health.record_success('hebcal')
            except (requests.RequestException, TimeoutError, ValueError):
                health.record_failure('hebcal')
                raise

            # Look for parasha in events
            for event in data.get('events', []):
                if 'parashat' in event.lower():
                    _PARASHA_CACHE[cache_key] = {"ts": now, "value": event}
                    return event

            no_parasha = "No parasha for this date"
            _PARASHA_CACHE[cache_key] = {"ts": now, "value": no_parasha}
            return no_parasha
        except Exception:
            logger.exception("Error getting parasha for %s", gregorian_date)
            return "Parasha lookup unavailable"

    # Pyluach's holiday() returns the SAME bare 'Succos'/'Pesach' string for
    # every day of each 7-8 day festival -- Yom Tov days and Chol HaMoed
    # (intermediate, work-permitted) days alike -- since it doesn't
    # distinguish them. That's fine for "is Musaf said today" (Musaf IS said
    # daily through Chol HaMoed), but wrong for candle-lighting/havdalah/
    # day-boundary purposes, which must fire only on the actual work-
    # prohibited Yom Tov days. Every other name in yom_tov_list spans just
    # its own Yom Tov day(s) with no Chol HaMoed in between, so only these
    # two need narrowing by Hebrew day-of-month.
    _YOM_TOV_PROPER_DAYS = {
        'Succos': {15, 16},         # Tishrei 15-16; 17-21 are Chol HaMoed/Hoshana Rabbah
        'Pesach': {15, 16, 21, 22},  # Nissan 15-16 & 21-22; 17-20 are Chol HaMoed
    }

    @staticmethod
    def is_holiday(gregorian_date=None):
        """Check if date is a holiday using Pyluach."""
        try:
            if gregorian_date is None:
                gregorian_date = date_lib.today()
            elif isinstance(gregorian_date, str):
                y, m, d = map(int, gregorian_date.split('-'))
                gregorian_date = date_lib(y, m, d)

            greg = dates.GregorianDate(
                gregorian_date.year,
                gregorian_date.month,
                gregorian_date.day
            )
            hebrew = greg.to_heb()

            holiday = hebrew.holiday()

            # Classify holiday type
            yom_tov_list = ['Rosh Hashana', 'Yom Kippur', 'Succos',
                            'Shmini Atzeres', 'Simchas Torah', 'Pesach', 'Shavuos']

            if holiday in yom_tov_list:
                holiday_type = 'Yom Tov'
            elif holiday:
                holiday_type = 'Minor Holiday'
            else:
                holiday_type = 'Regular Day'

            is_yom_tov_proper = holiday_type == 'Yom Tov'
            restricted_days = PyluachEngine._YOM_TOV_PROPER_DAYS.get(holiday)
            if restricted_days is not None:
                is_yom_tov_proper = hebrew.day in restricted_days

            return {
                'is_holiday': bool(holiday),
                'holiday_name': holiday,
                'holiday_type': holiday_type,
                'is_yom_tov_proper': is_yom_tov_proper,
            }
        except Exception as e:
            logger.exception("Error checking holiday for %s", gregorian_date)
            return {'is_holiday': False, 'holiday_name': None, 'error': str(e)}

    @staticmethod
    def hebrew_to_gregorian(hebrew_year, hebrew_month, hebrew_day):
        """Convert a Hebrew date to Gregorian using Pyluach (reverse of gregorian_to_hebrew).

        Added for plan.md §9 (backend/ai_tools.py's get_hebrew_date /
        calculate_hebrew_date_math tools) -- the class previously only
        supported the Gregorian->Hebrew direction.
        """
        try:
            hebrew = dates.HebrewDate(
                int(hebrew_year), int(hebrew_month), int(hebrew_day))
            gregorian = hebrew.to_pydate()
            return {
                'gregorian_date': gregorian.isoformat(),
                'hebrew_date': f"{hebrew.day} {hebrew.month_name()} {hebrew.year}",
                'hebrew_year': hebrew.year,
                'hebrew_month': hebrew.month,
                'hebrew_day': hebrew.day,
            }
        except Exception as e:
            logger.exception(
                "Error converting Hebrew %s/%s/%s to Gregorian",
                hebrew_year, hebrew_month, hebrew_day)
            return {'gregorian_date': None, 'error': str(e)}

    @staticmethod
    def add_days_to_hebrew_date(hebrew_year, hebrew_month, hebrew_day, days):
        """Add (or subtract, if negative) a number of days to a Hebrew date.

        Added for plan.md §9's calculate_hebrew_date_math tool -- pure
        Pyluach arithmetic (HebrewDate.__add__), no new date logic beyond
        what the library already provides.
        """
        try:
            start = dates.HebrewDate(
                int(hebrew_year), int(hebrew_month), int(hebrew_day))
            result = start + int(days)
            return {
                'hebrew_date': f"{result.day} {result.month_name()} {result.year}",
                'hebrew_year': result.year,
                'hebrew_month': result.month,
                'hebrew_day': result.day,
                'gregorian_date': result.to_pydate().isoformat(),
            }
        except Exception as e:
            logger.exception(
                "Error adding %s days to Hebrew %s/%s/%s",
                days, hebrew_year, hebrew_month, hebrew_day)
            return {'error': str(e)}

    @staticmethod
    def next_occurrence_of_hebrew_date(hebrew_month, hebrew_day, after_gregorian_date=None):
        """Find the next Gregorian date on which Hebrew month/day recurs
        (e.g. an upcoming yahrzeit or Hebrew birthday). Searches forward
        up to 3 Hebrew years to safely cross Adar/Adar-II leap-year edge
        cases for dates that only exist in a leap year.
        """
        try:
            if after_gregorian_date is None:
                anchor = date_lib.today()
            elif isinstance(after_gregorian_date, str):
                y, m, d = map(int, after_gregorian_date.split('-'))
                anchor = date_lib(y, m, d)
            else:
                anchor = after_gregorian_date

            anchor_greg = dates.GregorianDate(
                anchor.year, anchor.month, anchor.day)
            anchor_hebrew_year = anchor_greg.to_heb().year

            for offset in range(3):
                candidate_year = anchor_hebrew_year + offset
                try:
                    candidate = dates.HebrewDate(
                        candidate_year, int(hebrew_month), int(hebrew_day))
                except ValueError:
                    # This Hebrew month/day does not exist in this year
                    # (e.g. 30 Cheshvan/Kislev in a short year, or Adar II
                    # requested in a non-leap year) -- try the next year.
                    continue
                candidate_greg = candidate.to_pydate()
                if candidate_greg >= anchor:
                    return {
                        'gregorian_date': candidate_greg.isoformat(),
                        'hebrew_date': f"{candidate.day} {candidate.month_name()} {candidate.year}",
                        'hebrew_year': candidate.year,
                        'hebrew_month': candidate.month,
                        'hebrew_day': candidate.day,
                    }
            return {'error': 'Could not resolve a next occurrence within 3 Hebrew years'}
        except Exception as e:
            logger.exception(
                "Error finding next occurrence of Hebrew %s/%s after %s",
                hebrew_month, hebrew_day, after_gregorian_date)
            return {'error': str(e)}


# Create global engine instance
calendar_engine = PyluachEngine()
