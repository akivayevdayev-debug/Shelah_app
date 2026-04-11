"""
ShelahEngine service facade.

This module provides a single class that aggregates external data providers:
- Zmanim/monthly calendar events from zmanim_engine.
- Daily study and textual sources from sefaria/sefaria_library.
- Community customs and background lookups from customs/search.

app.py route handlers call this facade to keep endpoint code thin and consistent.
"""

import sefaria
import customs
import search
from sefaria_library import get_text
from zmanim_engine import get_community_zmanim, get_monthly_events


class ShelahEngine:
    def __init__(self, lat=40.7128, lon=-74.0060, timezone_str=None):
        self.lat = lat
        self.lon = lon
        self.tz = timezone_str

    def get_zmanim(self, community="standard"):
        """Returns the Zmanim dict for the dashboard"""
        return get_community_zmanim(self.lat, self.lon, self.tz, community)

    def get_monthly_zmanim(self):
        """Returns FullCalendar events"""
        return get_monthly_events(self.lat, self.lon, self.tz)

    def get_daily_learning(self):
        """Uses our robust Sefaria daily study function"""
        return sefaria.get_daily_study()

    def get_halachipedia_summary(self, topic):
        """Uses the previously defined search_halachipedia logic"""
        return search.search_halachipedia(topic)

    def get_customs(self, topic):
        """Fetch customs database"""
        return customs.search_customs(topic)

    def get_wiki(self, topic):
        """Fetch Wikipedia background"""
        return search.search_wikipedia(topic)

    def get_library_text(self, reference):
        """Sefaria API: Fetches BOTH English and Hebrew arrays for side-by-side reading"""
        try:
            # Reuse the centralized library client so caching and flattening are consistent.
            data = get_text(reference)
            if data.get("error"):
                return {"ref": reference, "lines": [{"he": "", "en": data.get("error", "Failed to fetch source.")}]}

            return {
                "ref": data.get("ref", reference),
                "lines": data.get("lines", [])
            }
        except Exception as e:
            print(f"[Engine] get_library_text error: {e}")
            return {"ref": reference, "lines": [{"he": "", "en": "Failed to fetch source."}]}
