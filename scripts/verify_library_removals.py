#!/usr/bin/env python3
"""Re-check the removals of the library leaf report against Sefaria-Export.

reports/library_leaf_remove_fix_report.full.json (crawl_library_leaves.py,
2026-04-21) removes 448 works from the library. Its crawl probed each work
at its bare title and at "Title 1" / "Title, 1"-shaped guesses; a
complex-schema work (a siddur, machzor, haggadah, most commentaries) only
answers at "Title, Section, Subsection", so every one of those probes was a
400 whatever the work holds. Not one removal in that report saw a work
load and come back empty.

This script asks the question the crawl couldn't: for each removal, does
Sefaria's own bulk export (reachable where www.sefaria.org is not) have the
work's schema -- so the library can build the ref it opens the work at --
and actual text for it? Works that pass are written to
reports/library_leaf_reinstated.json, which backend/sefaria_library.py
subtracts from the report's removals for that report run only; rerunning
the (fixed) crawler supersedes it.

    python3 scripts/verify_library_removals.py
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.sefaria_library import leaf_refs_from_schema  # noqa: E402

EXPORT_BASE = "https://storage.googleapis.com/sefaria-export"
EXPORT_LISTING = "https://storage.googleapis.com/storage/v1/b/sefaria-export/o"
REPORT_PATH = REPO_ROOT / "reports" / "library_leaf_remove_fix_report.full.json"
OUT_PATH = REPO_ROOT / "reports" / "library_leaf_reinstated.json"
# A text file smaller than this is an empty shell (metadata, no text).
MIN_TEXT_BYTES = 2048


def schema_url(title: str) -> str:
    return f"{EXPORT_BASE}/schemas/{urllib.parse.quote(title.replace(' ', '_'))}.json"


def text_prefix(row: Dict[str, Any]) -> str:
    """The export's folder for the work's text files: json/<categories>/<Title>/."""
    title = row["title"]
    categories = list(row.get("categories") or [])
    if categories and categories[-1] == title:
        categories = categories[:-1]
    return "json/" + "".join(f"{c}/" for c in categories) + f"{title}/"


def fetch_schema(session, title: str) -> Optional[Dict[str, Any]]:
    response = session.get(schema_url(title), timeout=30)
    if response.status_code != 200:
        return None
    payload = response.json()
    return payload if isinstance(payload, dict) else None


def text_file_sizes(session, row: Dict[str, Any]) -> List[int]:
    """Sizes of the work's text files in the export: under its category
    folder, else anywhere (a work filed under other categories there)."""
    attempts = (
        {"prefix": text_prefix(row)},
        {"prefix": "json/", "matchGlob": f"json/**/{row['title']}/**"},
    )
    for params in attempts:
        response = session.get(EXPORT_LISTING, params={**params, "maxResults": 50, "fields": "items(name,size)"},
                               timeout=60)
        response.raise_for_status()
        items = response.json().get("items") or []
        if items:
            return [int(item.get("size") or 0) for item in items]
    return []


def verify_row(row: Dict[str, Any], session) -> Dict[str, Any]:
    """{"title", "reinstate": bool, ...evidence} for one removal row."""
    title = row["title"]
    entry = fetch_schema(session, title)
    if entry is None:
        return {"title": title, "reinstate": False, "why": "no schema in the export"}
    schema = entry.get("schema")
    if not isinstance(schema, dict) or not schema.get("nodes"):
        # A simple-schema work loads at its bare title; the crawl's failure
        # there is real evidence.
        return {"title": title, "reinstate": False, "why": "simple schema; the crawl's probe was the right shape"}
    largest = max(text_file_sizes(session, row), default=0)
    if largest < MIN_TEXT_BYTES:
        return {"title": title, "reinstate": False, "why": "no text in the export"}
    return {
        "title": title,
        "reinstate": True,
        "opening_ref": leaf_refs_from_schema(schema, str(entry.get("title") or title), 1)[0],
        "text_bytes": largest,
    }


def build_payload(report: Dict[str, Any], results: List[Dict[str, Any]], now: datetime) -> Dict[str, Any]:
    reinstated = [{k: v for k, v in r.items() if k != "reinstate"} for r in results if r["reinstate"]]
    still_removed = [{"title": r["title"], "why": r["why"]} for r in results if not r["reinstate"]]
    return {
        "machine_generated": True,
        "generated_at_utc": now.isoformat(),
        "report": "reports/library_leaf_remove_fix_report.full.json",
        "report_generated_at_utc": report.get("generated_at_utc"),
        "source": EXPORT_BASE,
        "method": (
            "A removal is reinstated when the work has a complex schema in Sefaria-Export (so the "
            "report's bare-title probes could not have loaded it, and the library opens it at the "
            f"schema's first leaf, opening_ref) and at least {MIN_TEXT_BYTES} bytes of text there."
        ),
        "stats": {"removals": len(results), "reinstated": len(reinstated), "still_removed": len(still_removed)},
        "reinstated": reinstated,
        "still_removed": still_removed,
    }


def run(report: Dict[str, Any], session, workers: int = 16,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> Dict[str, Any]:
    rows = [row for row in report.get("removals") or [] if isinstance(row, dict) and row.get("title")]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda row: verify_row(row, session), rows))
    return build_payload(report, results, now())


def main(argv: Optional[List[str]] = None, session=None, *,
         report_path: Path = REPORT_PATH, output_path: Path = OUT_PATH) -> int:
    # The files this reads and writes are fixed (REPORT_PATH, OUT_PATH): the
    # command line takes no paths, so nothing a caller types can steer a read
    # or a write outside reports/. report_path/output_path are for the tests,
    # which point them at a temporary directory.
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.parse_args(argv)

    with open(report_path, encoding="utf-8") as handle:
        report = json.load(handle)
    if session is not None:
        payload = run(report, session)
    else:
        with requests.Session() as owned_session:
            payload = run(report, owned_session)
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    stats = payload["stats"]
    print(f"{stats['reinstated']} of {stats['removals']} removals reinstated -> {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
