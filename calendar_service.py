"""
Calendar Service - Pyluach-first date orchestrator for Sheilah
Primary source of truth for Hebrew/Gregorian conversions, holiday detection, and parasha lookups.
Validates against Hebcal API to ensure consistency across calendar systems.
"""

import logging
from pyluach import dates, utils
import requests
from datetime import datetime, date as date_lib

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class PyluachEngine:
    """Pyluach-first calendar engine with Hebcal validation."""

    # Mapping from Hebcal month names to Pyluach month names for normalization
    HEBCAL_TO_PYLUACH_MONTHS = {
        'Nisan': 'Nissan',
        'Tevet': 'Teves',
    }

    @staticmethod
    def normalize_month_name(month_name):
        """Normalize month name from Hebcal to Pyluach format."""
        return PyluachEngine.HEBCAL_TO_PYLUACH_MONTHS.get(month_name, month_name)

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

            # Use Hebcal converter API for parasha information in events array
            hebcal_url = f"https://www.hebcal.com/converter?g2h=on&gy={gregorian_date.year}&gm={gregorian_date.month}&gd={gregorian_date.day}&cfg=json"
            response = requests.get(hebcal_url, timeout=5)
            response.raise_for_status()
            data = response.json()

            # Look for parasha in events
            for event in data.get('events', []):
                if 'parashat' in event.lower():
                    return event

            return "No parasha for this date"
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

    @staticmethod
    def validate_against_hebcal(gregorian_date=None):
        """Cross-validate Hebrew date against Hebcal API."""
        try:
            if gregorian_date is None:
                gregorian_date = date_lib.today()
            elif isinstance(gregorian_date, str):
                y, m, d = map(int, gregorian_date.split('-'))
                gregorian_date = date_lib(y, m, d)

            # Get Pyluach date
            greg = dates.GregorianDate(
                gregorian_date.year,
                gregorian_date.month,
                gregorian_date.day
            )
            hebrew = greg.to_heb()
            month_name = hebrew.month_name()
            pyluach_hebrew = f"{hebrew.day} {month_name} {hebrew.year}"

            # Get Hebcal date
            try:
                hebcal_url = f"https://www.hebcal.com/converter?g2h=on&gy={gregorian_date.year}&gm={gregorian_date.month}&gd={gregorian_date.day}&cfg=json"
                response = requests.get(hebcal_url, timeout=5)
                response.raise_for_status()
                hebcal_data = response.json()

                # Note: Hebcal API returns hm as a string month name, not integer
                heb_month = hebcal_data.get('hm')  # String like "Nisan"
                heb_day = hebcal_data.get('hd')
                heb_year = hebcal_data.get('hy')
                hebcal_hebrew = f"{heb_day} {heb_month} {heb_year}"

                # Normalize Hebcal month names to match Pyluach format
                heb_month_normalized = PyluachEngine.normalize_month_name(
                    heb_month)
                pyluach_normalized = f"{hebrew.day} {month_name} {hebrew.year}"
                hebcal_normalized = f"{heb_day} {heb_month_normalized} {heb_year}"
                dates_match = pyluach_normalized == hebcal_normalized

                return {
                    'pyluach_hebrew': pyluach_hebrew,
                    'hebcal_hebrew': hebcal_hebrew,
                    'dates_match': dates_match,
                    'gregorian_date': str(gregorian_date),
                    'events': hebcal_data.get('events', [])
                }
            except Exception as api_err:
                logger.warning(f"Hebcal API unavailable: {api_err}")
                return {
                    'pyluach_hebrew': pyluach_hebrew,
                    'hebcal_hebrew': None,
                    'dates_match': None,
                    'gregorian_date': str(gregorian_date),
                    'hebcal_error': str(api_err)
                }
        except Exception as e:
            logger.error(
                f"Error validating against Hebcal for {gregorian_date}: {e}")
            return {'error': str(e), 'pyluach_hebrew': 'Error', 'hebcal_hebrew': None}


# Create global engine instance
calendar_engine = PyluachEngine()
