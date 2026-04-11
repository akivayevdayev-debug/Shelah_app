"""
Community customs data loader and matcher.

Responsibilities:
- Load all JSON files from customs/.
- Normalize different JSON shapes into a searchable in-memory structure.
- Perform keyword and fuzzy matching for minhag/custom responses.

Used by data_service.ShelahEngine.get_customs and exposed in API responses.
"""

import json
import os
import glob
import difflib

# Path to customs folder
CUSTOMS_DIR = os.path.join(os.path.dirname(__file__), "customs")


def load_all_customs():
    """Load all JSON files from customs folder"""
    customs = {}

    files = glob.glob(os.path.join(CUSTOMS_DIR, "*.json"))

    for filepath in files:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

                # Case 1: simple format (like customs_db.json)
                if isinstance(data, dict):
                    for community, topics in data.items():
                        if isinstance(topics, dict):
                            customs.setdefault(community, {}).update(topics)

                # Case 2: structured JSON (your large files)
                if "name" in data:
                    name = data.get("name", "Unknown")

                    customs[name] = {}

                    # Try to extract halacha_index
                    for item in data.get("halacha_index", []):
                        topic = item.get("topic", "").lower()
                        category = item.get("category", "").lower()

                        customs[name][f"{category}_{topic}"] = {
                            "keywords": [
                                topic,
                                category
                            ],
                            "ruling": item.get("summary", ""),
                            "source": ", ".join(data.get("source_registry", {}).get("primary", [])),
                            "notes": " | ".join(item.get("common_practices", [])[:2]),
                            "media_url": item.get("media_url", "")
                        }

                    # Add unique minhagim
                    if "unique_minhagim" in data:
                        customs[name]["unique"] = {
                            "keywords": ["custom", "minhag", name.lower()],
                            "ruling": " | ".join(data["unique_minhagim"].get("examples", [])),
                            "source": "Community tradition",
                            "notes": data["unique_minhagim"].get("notes", "")
                        }

        except Exception as e:
            print(f"[Customs Load Error] {filepath}: {e}")

    return customs


def search_customs(question):
    """Search all customs for relevant entries using exact and fuzzy matching"""
    customs = load_all_customs()
    q_lower = question.lower()
    q_words = q_lower.split()
    matches = []

    for community, topics in customs.items():
        if not isinstance(topics, dict):
            continue

        for topic, data in topics.items():
            if not isinstance(data, dict):
                continue

            keywords = data.get("keywords", [])

            # Exact matches
            exact_match = (
                any(kw in q_lower for kw in keywords if kw)
                or community.lower() in q_lower
                or topic.replace("_", " ") in q_lower
            )

            # Fuzzy matching
            fuzzy_match = False
            for kw in keywords:
                if kw and difflib.get_close_matches(kw, q_words, n=1, cutoff=0.8):
                    fuzzy_match = True
                    break

            if exact_match or fuzzy_match:
                matches.append({
                    "community": community,
                    "topic": topic,
                    "ruling": data.get("ruling", ""),
                    "source": data.get("source", ""),
                    "notes": data.get("notes", ""),
                    "media_url": data.get("media_url", "")
                })

    return matches
