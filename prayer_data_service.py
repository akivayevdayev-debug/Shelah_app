"""Prayer data loader that consolidates all prayer datasets into one source of truth."""

from __future__ import annotations

import json
import os
from functools import lru_cache

from siddur_sefard import PRAYERS_DATA as PRIMARY_PRAYERS

SUPPORTED_LANGS = ("en", "he", "ar", "ru")


def _normalize_prayer_entry(name: str, entry: dict) -> dict:
    normalized = {}
    for lang in SUPPORTED_LANGS:
        value = entry.get(lang, "") if isinstance(entry, dict) else ""
        if isinstance(value, str):
            value = value.strip()
        else:
            value = str(value).strip()

        if not value and lang != "en":
            # Keep language keys non-empty so client-side switches always render content.
            value = entry.get("en", "") if isinstance(entry, dict) else ""
            value = f"[{lang}] {value}".strip()

        normalized[lang] = value

    # Guarantee an English fallback even if malformed source data is encountered.
    if not normalized["en"]:
        normalized["en"] = f"{name} content is currently unavailable."

    return normalized


def _merge_prayers(target: dict, incoming: dict) -> None:
    for name, entry in (incoming or {}).items():
        if not isinstance(entry, dict):
            continue

        base = target.get(name, {})
        merged = dict(base)
        for lang in SUPPORTED_LANGS:
            incoming_value = entry.get(lang)
            if isinstance(incoming_value, str) and incoming_value.strip() and not merged.get(lang):
                merged[lang] = incoming_value.strip()
            elif lang not in merged:
                merged[lang] = ""

        target[name] = _normalize_prayer_entry(name, merged)


@lru_cache(maxsize=1)
def get_prayers_data() -> dict:
    """Load and merge prayer data from canonical source with optional JSON fallback."""
    merged = {}
    _merge_prayers(merged, PRIMARY_PRAYERS)

    # Integrate legacy JSON dataset when available.
    json_path = os.path.join(os.path.dirname(
        __file__), "sefardic_prayers.json")
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                json_data = json.load(f)
            _merge_prayers(merged, json_data)
        except Exception:
            pass

    return merged
