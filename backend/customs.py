"""
Community customs data loader and matcher.

Responsibilities:
- Load all JSON files from customs/.
- Normalize different JSON shapes into a searchable in-memory structure.
- Perform keyword and fuzzy matching for minhag/custom responses.

Prompt 35 STEP 4 (plan.md §22.3.4) audit: this JSON path is NOT authoritative
for the main /ask pre-fetch pipeline any more -- that pipeline gets its
customs context from the Supabase `community_knowledge` table (populated
by scripts/migrate_customs_to_supabase.py) via
backend/rag.py::_retrieve_community_knowledge() + _knowledge_rows_to_customs()
(app.py's _collect_ask_question_context / asgi.py's async equivalent).
data_service.ShelahEngine.get_customs(), the wrapper this docstring used to
cite as the production caller, has zero production callers left -- only
tests/test_small_gap_modules.py exercises it directly.

This module's search_customs()/load_all_customs() ARE still live, though:
they're the sole implementation backing backend/ai_tools.py's
search_community_customs agentic tool. That tool can't call
_retrieve_community_knowledge() instead -- backend/rag.py does a lazy
`import app as _app` internally (a deliberate circular-import dodge
documented at backend/rag.py's _retrieve_community_knowledge), and
backend/ai_tools.py's contract is backend.* imports only, never app (see
_h_search_community_customs's own docstring). So keep this module: deleting
it would break the agentic tool's community-customs lookup outright.

The accepted trade-off: the customs/*.json files were the migration
script's *source*, not a live mirror of Supabase -- edits made directly in
the community_knowledge table afterward will NOT reach this path, so the
agentic tool's customs answers can drift from the pre-fetch path's. Fixing
that means letting backend/ai_tools.py reach a Supabase-backed query
without importing app.py (e.g. hoisting _retrieve_community_knowledge's
Supabase-client access out of backend/rag.py so it no longer needs the
`import app as _app` workaround) -- out of scope here; flagged as a
follow-up, not silently left unexplained.
"""

import json
import logging
import os
import glob
import difflib
from pathlib import Path

logger = logging.getLogger(__name__)

# Path to customs folder
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CUSTOMS_DIR = str(PROJECT_ROOT / "customs")

# Required top-level fields for structured community files (v2.x)
_REQUIRED_FIELDS = ("heritage_id", "name", "halacha_index")


def _validate_customs_file(data: dict) -> list[str]:
    """Return a list of validation error strings for a structured customs file."""
    errors = []
    if not isinstance(data, dict):
        errors.append("root value is not a JSON object")
        return errors
    for field in _REQUIRED_FIELDS:
        if field not in data:
            errors.append(f"missing required field '{field}'")
        elif not data[field]:
            errors.append(f"required field '{field}' is empty")
    return errors


def validate_all_customs_at_startup() -> None:
    """Validate all community JSON files at startup.  Logs an error per offending
    file without raising, so a single bad file does not crash the entire app."""
    files = sorted(glob.glob(os.path.join(CUSTOMS_DIR, "*.json")))
    skip = {"customs_db.json", "schema.json"}
    for filepath in files:
        if os.path.basename(filepath).lower() in skip:
            continue
        try:
            with open(filepath, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            logger.exception("customs validation: cannot parse %s", filepath)
            continue
        if "name" not in data:
            continue  # Legacy flat-dict format — skip schema check
        errors = _validate_customs_file(data)
        if errors:
            logger.error(
                "customs validation: %s failed schema check — %s",
                os.path.basename(filepath),
                "; ".join(errors),
            )
_CUSTOMS_CACHE: dict = {
    "signature": (),
    "data": {},
}


def _build_customs_signature(files):
    """Create a cheap change signature from file names and mtimes."""
    signature = []
    for filepath in files:
        try:
            signature.append((os.path.basename(filepath),
                             os.path.getmtime(filepath)))
        except Exception:
            signature.append((os.path.basename(filepath), -1.0))
    return tuple(sorted(signature))


def _build_trusted_sources(data):
    """Collect trustworthy sources from each community JSON."""
    if not isinstance(data, dict):
        return []

    candidates = []

    source_registry = data.get("source_registry", {}) if isinstance(
        data.get("source_registry"), dict) else {}
    candidates.extend(source_registry.get("primary", []) if isinstance(
        source_registry.get("primary"), list) else [])

    authorities = data.get("core_halachic_authorities", {}) if isinstance(
        data.get("core_halachic_authorities"), dict) else {}
    for key in (
        "primary_codes",
        "major_rishonim_base",
        "later_ashkenazi_poskim",
        "later_sephardi_poskim",
        "later_moroccan_poskim",
        "later_turkish_poskim",
    ):
        values = authorities.get(key)
        if isinstance(values, list):
            candidates.extend(values)

    deduped = []
    seen = set()
    for source in candidates:
        value = str(source or "").strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(value)

    return deduped[:6]


def _merge_simple_format_customs(customs, data):
    """Case 1: simple {community: {topic: entry}} JSON shape, merged in place.
    Split out of load_all_customs() to keep this loop out of that function's
    own complexity count (SonarCloud python:S3776).
    """
    for community, topics in data.items():
        if isinstance(topics, dict):
            customs.setdefault(community, {}).update(topics)


def _build_structured_customs_entry(data):
    """Case 2: structured community JSON (name/halacha_index/unique_minhagim).
    Returns (name, entries_dict). Split out of load_all_customs() (SonarCloud
    python:S3776) -- see _merge_simple_format_customs.
    """
    name = data.get("name", "Unknown")
    trusted_sources = _build_trusted_sources(data)
    entries = {}

    # Try to extract halacha_index
    for item in data.get("halacha_index", []):
        topic = item.get("topic", "").lower()
        category = item.get("category", "").lower()

        entries[f"{category}_{topic}"] = {
            "keywords": [
                topic,
                category
            ],
            "ruling": item.get("summary", ""),
            "source": item.get("source", "") or ", ".join(trusted_sources[:4]),
            "notes": " | ".join(item.get("common_practices", [])[:2]),
            "media_url": item.get("media_url", "")
        }

    # Add unique minhagim
    if "unique_minhagim" in data:
        entries["unique"] = {
            "keywords": ["custom", "minhag", name.lower()],
            "ruling": " | ".join(data["unique_minhagim"].get("examples", [])),
            "source": "Community tradition",
            "notes": data["unique_minhagim"].get("notes", "")
        }

    return name, entries


def load_all_customs():
    """Load all JSON files from customs folder"""
    files = sorted(glob.glob(os.path.join(CUSTOMS_DIR, "*.json")))
    signature = _build_customs_signature(files)
    if _CUSTOMS_CACHE.get("signature") == signature and isinstance(_CUSTOMS_CACHE.get("data"), dict):
        return _CUSTOMS_CACHE["data"]

    customs = {}

    for filepath in files:
        if os.path.basename(filepath).lower() == "customs_db.json":
            # Legacy dataset is retired from active community/customs browsing.
            continue
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

                # Case 1: simple format (like customs_db.json)
                if isinstance(data, dict):
                    _merge_simple_format_customs(customs, data)

                # Case 2: structured JSON (your large files)
                if "name" in data:
                    name, entries = _build_structured_customs_entry(data)
                    customs[name] = entries

        except Exception as e:
            print(f"[Customs Load Error] {filepath}: {e}")

    _CUSTOMS_CACHE["signature"] = signature
    _CUSTOMS_CACHE["data"] = customs
    return customs


def _customs_entry_matches(topic, data, q_lower, q_words, community_lower):
    """Exact-or-fuzzy match test for one customs entry. Split out of
    search_customs() to keep this per-entry branching out of that function's
    own complexity count (SonarCloud python:S3776).
    """
    keywords = data.get("keywords", [])

    exact_match = (
        any(kw in q_lower for kw in keywords if kw)
        or community_lower in q_lower
        or topic.replace("_", " ") in q_lower
    )
    if exact_match:
        return True

    for kw in keywords:
        if kw and difflib.get_close_matches(kw, q_words, n=1, cutoff=0.8):
            return True
    return False


def search_customs(question):
    """Search all customs for relevant entries using exact and fuzzy matching"""
    customs = load_all_customs()
    q_lower = question.lower()
    q_words = q_lower.split()
    matches = []

    for community, topics in customs.items():
        if not isinstance(topics, dict):
            continue

        community_lower = community.lower()
        for topic, data in topics.items():
            if not isinstance(data, dict):
                continue

            if not _customs_entry_matches(topic, data, q_lower, q_words, community_lower):
                continue

            matches.append({
                "community": community,
                "topic": topic,
                "ruling": data.get("ruling", ""),
                "source": data.get("source", ""),
                "notes": data.get("notes", ""),
                "media_url": data.get("media_url", "")
            })

    return matches
