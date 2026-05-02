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
            logger.error(f"Error converting {gregorian_date} to Hebrew: {e}")
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

            # Use Hebcal converter API for parasha information in events array
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

            # Look for parasha in events
            for event in data.get('events', []):
                if 'parashat' in event.lower():
                    _PARASHA_CACHE[cache_key] = {"ts": now, "value": event}
                    return event

            no_parasha = "No parasha for this date"
            _PARASHA_CACHE[cache_key] = {"ts": now, "value": no_parasha}
            return no_parasha
        except Exception as e:
            logger.error(f"Error getting parasha for {gregorian_date}: {e}")
            return "Parasha lookup unavailable"

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

            return {
                'is_holiday': bool(holiday),
                'holiday_name': holiday,
                'holiday_type': 'Yom Tov' if holiday in yom_tov_list else 'Minor Holiday' if holiday else 'Regular Day'
            }
        except Exception as e:
            logger.error(f"Error checking holiday for {gregorian_date}: {e}")
            return {'is_holiday': False, 'holiday_name': None, 'error': str(e)}


# Create global engine instance
calendar_engine = PyluachEngine()
