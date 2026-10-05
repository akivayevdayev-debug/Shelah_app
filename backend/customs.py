"""
Community customs data loader and matcher.

Responsibilities:
- Load all JSON files from customs/.
- Normalize different JSON shapes into a searchable in-memory structure.
- Perform keyword and fuzzy matching for minhag/custom responses.

How the two customs stores relate:
- customs/*.json is the source of truth. scripts/migrate_customs_to_supabase.py
  seeds the Supabase `community_knowledge` table from it (deterministic upserts,
  `community_name` = each file's runtime.lens_key), and the main /ask pre-fetch
  reads that table via backend/rag.py::_retrieve_community_knowledge() +
  _knowledge_rows_to_customs(). An edited file is therefore not live in /ask
  until it has been migrated.
- This module reads the JSON directly. search_customs()/load_all_customs() back
  backend/ai_tools.py's search_community_customs agentic tool, and
  runtime_config() feeds the prompt's community lens (backend/claude.py) with each
  community's practice baseline instead of a hard-coded copy. ai_tools cannot call
  rag.py instead: that module lazily does `import app as _app` (a deliberate
  circular-import dodge), and ai_tools' contract is backend.* imports only.
- Edits made directly in the Supabase table are not reflected here. Fix the JSON
  and re-run the migration instead.
"""

import json
import logging
import os
import re
import glob
import difflib
from pathlib import Path

logger = logging.getLogger(__name__)

# Path to customs folder
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CUSTOMS_DIR = str(PROJECT_ROOT / "customs")

# Files in customs/ that are not community data.
_NON_COMMUNITY_FILES = frozenset({"customs_db.json", "schema.json"})

# Required top-level fields for structured community files (v2.x and 3.0)
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
    for filepath in files:
        if os.path.basename(filepath).lower() in _NON_COMMUNITY_FILES:
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


def _collect_trusted_authority_candidates(data):
    """Gather raw (possibly duplicate/blank) source-name candidates from a
    community file's source_registry and core_halachic_authorities blocks.
    Split out of _build_trusted_sources() (SonarCloud python:S3776).
    """
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
        "later_poskim",
        "later_ashkenazi_poskim",
        "later_sephardi_poskim",
        "later_moroccan_poskim",
        "later_turkish_poskim",
    ):
        values = authorities.get(key)
        if isinstance(values, list):
            candidates.extend(values)

    return candidates


# The research files describe each authority in a sentence ("Rema, cited where
# Moroccan custom coincides with him (e.g., kitniyot) but not as the binding
# code") and record what they could not find ("Not documented in the sources
# reviewed"). The full text stays in the file; a source label is the name only.
_NEGATIVE_STATEMENT_RE = re.compile(r"^(?:not|no|none|n/a)\b", re.IGNORECASE)
_FOR_PREFIX_RE = re.compile(r"^for [^:]{1,60}:\s*(?=\S)", re.IGNORECASE)
_AUTHORITY_NAME_END_RE = re.compile(r"\s+\(|\s+[\u2014\u2013]\s+|[;:,]\s")


def _authority_name(text):
    """The authority's name from a descriptive entry, or "" for a statement
    that records an absence rather than names an authority."""
    label = str(text or "").strip()
    if not label or _NEGATIVE_STATEMENT_RE.match(label):
        return ""
    label = _FOR_PREFIX_RE.sub("", label)
    name = _AUTHORITY_NAME_END_RE.split(label, maxsplit=1)[0].strip()
    return name or label


def _dedupe_source_labels(candidates):
    """Reduce raw source-name candidates to authority names, drop blanks and
    case-insensitive duplicates, preserving first-seen order."""
    deduped = []
    seen = set()
    for item in candidates:
        label = _authority_name(item)
        if not label:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(label)
    return deduped


def _build_trusted_sources(data):
    """Collect trustworthy sources from each community JSON (at most 6).
    Also re-exported by app.py as _build_trusted_custom_sources."""
    if not isinstance(data, dict):
        return []

    return _dedupe_source_labels(_collect_trusted_authority_candidates(data))[:6]


def _merge_simple_format_customs(customs, data):
    """Case 1: simple {community: {topic: entry}} JSON shape, merged in place.
    Split out of load_all_customs() to keep this loop out of that function's
    own complexity count (SonarCloud python:S3776).
    """
    for community, topics in data.items():
        if isinstance(topics, dict):
            customs.setdefault(community, {}).update(topics)


def _practice_notes(item):
    """Supporting text for one halacha_index entry: its first practices, then
    any subgroup variants. The entry's own `notes` field is reviewer
    provenance and is deliberately not included."""
    practices = [str(p) for p in (item.get("common_practices") or [])[:3]]
    variants = []
    for variant in item.get("variants") or []:
        if not isinstance(variant, dict):
            continue
        subgroup = str(variant.get("subgroup") or "").strip()
        practice = str(variant.get("practice") or "").strip()
        if practice:
            variants.append(f"{subgroup}: {practice}" if subgroup else practice)
    return " | ".join(practices + variants[:3])


def _unique_minhagim_entries(unique, name):
    """Entries for a community's distinctive customs.

    Two shapes exist: the 3.0 list of {name, description, when, source,
    confidence} objects (one entry each), and the legacy {examples, notes}
    dict (one combined entry under "unique")."""
    if isinstance(unique, list):
        entries = {}
        for item in unique:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            label = str(item["name"]).strip()
            entries[f"unique_{label.lower()}"] = {
                "keywords": [label.lower(), "custom", "minhag", name.lower()],
                "ruling": str(item.get("description") or ""),
                "source": str(item.get("source") or "Community tradition"),
                "notes": str(item.get("when") or ""),
                "confidence": str(item.get("confidence") or ""),
            }
        return entries
    if isinstance(unique, dict):
        return {"unique": {
            "keywords": ["custom", "minhag", name.lower()],
            "ruling": " | ".join(unique.get("examples", [])),
            "source": "Community tradition",
            "notes": unique.get("notes", ""),
        }}
    return {}


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
            "notes": _practice_notes(item),
            "confidence": item.get("confidence", ""),
            "media_url": item.get("media_url", "")
        }

    # Add unique minhagim
    if "unique_minhagim" in data:
        entries.update(_unique_minhagim_entries(data["unique_minhagim"], name))

    return name, entries


def load_all_customs():
    """Load all JSON files from customs folder"""
    files = sorted(glob.glob(os.path.join(CUSTOMS_DIR, "*.json")))
    signature = _build_customs_signature(files)
    if _CUSTOMS_CACHE.get("signature") == signature and isinstance(_CUSTOMS_CACHE.get("data"), dict):
        return _CUSTOMS_CACHE["data"]

    customs = {}

    for filepath in files:
        if os.path.basename(filepath).lower() in _NON_COMMUNITY_FILES:
            # customs_db.json is the retired legacy dataset; schema.json describes the format.
            continue
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

                # Case 2: structured community JSON. Checked first so its
                # dict-valued blocks (identity, runtime, ...) are not also
                # merged below as if each were a community.
                if isinstance(data, dict) and "name" in data:
                    name, entries = _build_structured_customs_entry(data)
                    customs[name] = entries

                # Case 1: simple {community: {topic: entry}} format
                elif isinstance(data, dict):
                    _merge_simple_format_customs(customs, data)

        except Exception as e:
            logger.exception("[Customs Load Error] %s: %s", filepath, e)

    _CUSTOMS_CACHE["signature"] = signature
    _CUSTOMS_CACHE["data"] = customs
    return customs


_RUNTIME_CACHE: dict = {
    "signature": None,
    "data": {},
}


def runtime_config():
    """Per-community values the code reads from the customs files instead of
    hard-coding them: {lens_key.lower(): {"name", "lens_key", "practice_baseline",
    "parameters"}}. Only files that declare runtime.lens_key appear. Re-read
    when a file's name or modification time changes, like load_all_customs()."""
    files = sorted(glob.glob(os.path.join(CUSTOMS_DIR, "*.json")))
    signature = _build_customs_signature(files)
    if _RUNTIME_CACHE["signature"] == signature:
        return _RUNTIME_CACHE["data"]

    config = {}
    for filepath in files:
        if os.path.basename(filepath).lower() in _NON_COMMUNITY_FILES:
            continue
        try:
            with open(filepath, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            logger.exception("[Customs Runtime Error] %s", filepath)
            continue
        runtime = data.get("runtime") if isinstance(data, dict) else None
        lens_key = str((runtime or {}).get("lens_key") or "").strip() if isinstance(runtime, dict) else ""
        if not lens_key:
            continue
        parameters = runtime.get("parameters")
        config[lens_key.lower()] = {
            "name": str(data.get("name") or lens_key),
            "lens_key": lens_key,
            "practice_baseline": str(runtime.get("practice_baseline") or "").strip(),
            "parameters": parameters if isinstance(parameters, list) else [],
        }

    _RUNTIME_CACHE["signature"] = signature
    _RUNTIME_CACHE["data"] = config
    return config


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


def _customs_entry_score(topic, data, q_lower):
    """Relevance of a matched entry: a topic or keyword named in the question
    outranks an entry that matched only because the community was named, so a
    community with dozens of entries returns the right ones first."""
    score = 0
    if topic.replace("_", " ") in q_lower:
        score += 3
    for kw in data.get("keywords", []):
        if kw and kw in q_lower:
            score += 2
    return score


def search_customs(question):
    """Search all customs for relevant entries using exact and fuzzy matching.
    Best matches first."""
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
                "confidence": data.get("confidence", ""),
                "media_url": data.get("media_url", ""),
                "_score": _customs_entry_score(topic, data, q_lower),
            })

    matches.sort(key=lambda m: -m["_score"])  # stable: file order breaks ties
    for match in matches:
        del match["_score"]
    return matches
