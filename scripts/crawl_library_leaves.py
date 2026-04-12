#!/usr/bin/env python3
"""
Crawl Sefaria library leaf titles and produce a machine-generated remove/fix report.

What this script does:
1) Downloads the full Sefaria index tree.
2) Collects every leaf node that has a title.
3) Probes each leaf for loadability using direct refs + conservative fallback candidates.
4) Writes a JSON report with suggested "fix" refs or "remove" recommendations.

Example usage:
    /opt/homebrew/bin/python3 scripts/crawl_library_leaves.py \
        --output reports/library_leaf_remove_fix_report.json

Quick sample run:
    /opt/homebrew/bin/python3 scripts/crawl_library_leaves.py \
        --max-leaves 150 \
        --output reports/library_leaf_remove_fix_report.sample.json \
        --verbose
"""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

import requests

SEFARIA_API = "https://www.sefaria.org/api"


def normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def has_nonempty_text(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(has_nonempty_text(item) for item in value)
    return False


def encode_name_path(title: str) -> str:
    raw = str(title or "").strip().replace(" ", "_")
    return quote(raw, safe="_,.%'()-")


def encode_text_ref_path(ref: str) -> str:
    raw = str(ref or "").strip()
    raw = raw.replace(" ", "_").replace(":", ".").replace("/", "_")
    raw = raw.replace("&", "%26")
    return quote(raw, safe="_,.%'()-")


def get_children(node: Dict[str, Any]) -> List[Dict[str, Any]]:
    contents = node.get("contents")
    if isinstance(contents, list) and contents:
        return [child for child in contents if isinstance(child, dict)]

    children = node.get("children")
    if isinstance(children, list) and children:
        return [child for child in children if isinstance(child, dict)]

    return []


def collect_leaf_nodes(index_payload: Any) -> List[Dict[str, Any]]:
    leaves: List[Dict[str, Any]] = []

    def walk(node: Any, path: Sequence[str]) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child, path)
            return

        if not isinstance(node, dict):
            return

        label = str(node.get("title") or node.get(
            "category") or node.get("key") or "").strip()
        next_path = list(path)
        if label:
            next_path.append(label)

        children = get_children(node)
        if children:
            for child in children:
                walk(child, next_path)
            return

        title = str(node.get("title") or "").strip()
        if not title:
            return

        categories = node.get("categories")
        if not isinstance(categories, list):
            categories = []

        leaf = {
            "title": title,
            "he_title": str(node.get("heTitle") or node.get("heCategory") or "").strip(),
            "categories": [str(item).strip() for item in categories if str(item).strip()],
            "first_section_ref": str(node.get("firstSectionRef") or "").strip(),
            "ref": str(node.get("ref") or "").strip(),
            "path": next_path,
        }
        leaves.append(leaf)

    walk(index_payload, [])
    return leaves


def dedupe_leaf_titles(leaves: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    duplicates = 0

    for leaf in leaves:
        title = str(leaf.get("title") or "").strip()
        key = normalize_key(title)
        if not key:
            continue
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        deduped.append(leaf)

    return deduped, duplicates


def resolve_name_ref(
    session: requests.Session,
    title: str,
    timeout_seconds: float,
    cache: Dict[str, str],
) -> str:
    lookup = normalize_key(title)
    if not lookup:
        return ""

    if lookup in cache:
        return cache[lookup]

    url = f"{SEFARIA_API}/name/{encode_name_path(title)}"
    try:
        resp = session.get(url, timeout=timeout_seconds)
    except requests.RequestException:
        cache[lookup] = ""
        return ""

    if resp.status_code != 200:
        cache[lookup] = ""
        return ""

    try:
        payload = resp.json()
    except ValueError:
        cache[lookup] = ""
        return ""

    result_ref = ""
    if isinstance(payload, dict):
        maybe_ref = str(payload.get("ref") or "").strip()
        if payload.get("is_ref") and maybe_ref:
            result_ref = maybe_ref

    cache[lookup] = result_ref
    return result_ref


def probe_ref(
    session: requests.Session,
    ref: str,
    timeout_seconds: float,
    cache: Dict[str, Tuple[bool, str]],
) -> Tuple[bool, str]:
    candidate = str(ref or "").strip()
    if not candidate:
        return False, "empty_ref"

    if candidate in cache:
        return cache[candidate]

    url = f"{SEFARIA_API}/texts/{encode_text_ref_path(candidate)}"
    try:
        resp = session.get(
            url,
            params={"lang": "bi", "context": 0, "pad": 0},
            timeout=timeout_seconds,
        )
    except requests.RequestException as exc:
        result = (False, f"request_error:{exc.__class__.__name__}")
        cache[candidate] = result
        return result

    if resp.status_code != 200:
        result = (False, f"status_{resp.status_code}")
        cache[candidate] = result
        return result

    try:
        payload = resp.json()
    except ValueError:
        result = (False, "invalid_json")
        cache[candidate] = result
        return result

    if isinstance(payload, dict) and payload.get("error"):
        result = (False, "api_error")
        cache[candidate] = result
        return result

    has_text = False
    if isinstance(payload, dict):
        has_text = has_nonempty_text(payload.get(
            "text")) or has_nonempty_text(payload.get("he"))

    result = (has_text, "ok" if has_text else "no_text")
    cache[candidate] = result
    return result


def add_candidate(candidates: List[str], value: str) -> None:
    clean = str(value or "").strip()
    if clean and clean not in candidates:
        candidates.append(clean)


def build_primary_candidates(leaf: Dict[str, Any], name_ref: str) -> List[str]:
    candidates: List[str] = []
    add_candidate(candidates, leaf.get("first_section_ref") or "")
    add_candidate(candidates, leaf.get("ref") or "")
    add_candidate(candidates, name_ref)
    add_candidate(candidates, leaf.get("title") or "")
    return candidates


def build_heuristic_candidates(leaf: Dict[str, Any], existing: Sequence[str]) -> List[str]:
    title = str(leaf.get("title") or "").strip()
    if not title:
        return []

    categories = " ".join(str(item)
                          for item in (leaf.get("categories") or [])).lower()
    title_l = title.lower()

    candidates: List[str] = []

    add_candidate(candidates, f"{title} 1")
    add_candidate(candidates, f"{title} 1:1")
    add_candidate(candidates, f"{title}, 1")
    add_candidate(candidates, f"{title}, 1:1")

    if "talmud" in categories:
        add_candidate(candidates, f"{title} 2a")

    if "mishnah" in categories and not title_l.startswith("mishnah "):
        add_candidate(candidates, f"Mishnah {title} 1")

    if "commentary" in categories or " on " in title_l:
        add_candidate(candidates, f"{title}, Genesis 1")
        add_candidate(candidates, f"{title}, Genesis 1:1")

    if "targum" in categories or "tafsir" in categories or "targum" in title_l or "tafsir" in title_l:
        add_candidate(candidates, f"{title}, Genesis 1")
        add_candidate(candidates, f"{title}, Genesis 1:1")

    existing_set = {str(item).strip() for item in existing}
    return [candidate for candidate in candidates if candidate not in existing_set]


def first_nonempty(values: Sequence[str]) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def analyze_leaf(
    session: requests.Session,
    leaf: Dict[str, Any],
    timeout_seconds: float,
    name_cache: Dict[str, str],
    probe_cache: Dict[str, Tuple[bool, str]],
) -> Dict[str, Any]:
    title = str(leaf.get("title") or "").strip()
    name_ref = resolve_name_ref(session, title, timeout_seconds, name_cache)

    attempts: List[Dict[str, Any]] = []
    success_ref = ""
    success_phase = ""

    primary_candidates = build_primary_candidates(leaf, name_ref)
    for candidate in primary_candidates:
        ok, reason = probe_ref(
            session, candidate, timeout_seconds, probe_cache)
        attempts.append({"phase": "primary", "ref": candidate,
                        "ok": ok, "reason": reason})
        if ok:
            success_ref = candidate
            success_phase = "primary"
            break

    if not success_ref:
        heuristic_candidates = build_heuristic_candidates(
            leaf, primary_candidates)
        for candidate in heuristic_candidates:
            ok, reason = probe_ref(
                session, candidate, timeout_seconds, probe_cache)
            attempts.append(
                {"phase": "heuristic", "ref": candidate, "ok": ok, "reason": reason})
            if ok:
                success_ref = candidate
                success_phase = "heuristic"
                break

    initial_ref = first_nonempty([
        str(leaf.get("first_section_ref") or ""),
        str(leaf.get("ref") or ""),
        title,
    ])

    action = "remove"
    if success_ref:
        if normalize_key(success_ref) == normalize_key(initial_ref):
            action = "keep"
        else:
            action = "fix"

    return {
        "title": title,
        "he_title": str(leaf.get("he_title") or "").strip(),
        "categories": list(leaf.get("categories") or []),
        "path": list(leaf.get("path") or []),
        "initial_ref": initial_ref,
        "name_ref": name_ref,
        "action": action,
        "suggested_ref": success_ref,
        "resolution_phase": success_phase,
        "attempts": attempts,
    }


def fetch_index_payload(session: requests.Session, timeout_seconds: float) -> Any:
    url = f"{SEFARIA_API}/index"
    resp = session.get(url, timeout=timeout_seconds)
    resp.raise_for_status()
    payload = resp.json()

    if isinstance(payload, dict) and isinstance(payload.get("contents"), list):
        return payload["contents"]

    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawl library leaf titles and generate remove/fix report.",
    )
    parser.add_argument(
        "--output",
        default="reports/library_leaf_remove_fix_report.json",
        help="Output report path (JSON).",
    )
    parser.add_argument(
        "--max-leaves",
        type=int,
        default=0,
        help="Optional limit for leaf processing (0 means all).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=8.0,
        help="HTTP timeout per request in seconds.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="Optional sleep between leaf probes in seconds.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=100,
        help="Progress log interval (in leaves).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-leaf progress logs.",
    )
    parser.add_argument(
        "--include-kept",
        action="store_true",
        help="Include kept entries in report (can be very large).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    started_at = time.time()
    session = requests.Session()

    print("[crawler] Fetching Sefaria index tree...")
    index_payload = fetch_index_payload(session, args.timeout)

    all_leaves = collect_leaf_nodes(index_payload)
    deduped_leaves, duplicate_count = dedupe_leaf_titles(all_leaves)

    if args.max_leaves and args.max_leaves > 0:
        deduped_leaves = deduped_leaves[: args.max_leaves]

    total = len(deduped_leaves)
    print(
        f"[crawler] Leaves discovered: {len(all_leaves)} | unique titles: {len(deduped_leaves)} | duplicates removed: {duplicate_count}"
    )

    name_cache: Dict[str, str] = {}
    probe_cache: Dict[str, Tuple[bool, str]] = {}

    fixes: List[Dict[str, Any]] = []
    removals: List[Dict[str, Any]] = []
    kept: List[Dict[str, Any]] = []

    for idx, leaf in enumerate(deduped_leaves, start=1):
        result = analyze_leaf(session, leaf, args.timeout,
                              name_cache, probe_cache)
        action = result.get("action")

        if action == "fix":
            fixes.append(result)
        elif action == "remove":
            removals.append(result)
        elif args.include_kept:
            kept.append(result)

        if args.verbose or (idx % max(1, args.progress_every) == 0) or idx == total:
            print(
                f"[crawler] {idx}/{total} title='{result.get('title')}' action={action} suggested='{result.get('suggested_ref')}'"
            )

        if args.sleep > 0:
            time.sleep(args.sleep)

    elapsed_seconds = round(time.time() - started_at, 3)
    keep_count = total - len(fixes) - len(removals)

    report: Dict[str, Any] = {
        "machine_generated": True,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": {
            "api": SEFARIA_API,
            "index_endpoint": f"{SEFARIA_API}/index",
            "text_endpoint_template": f"{SEFARIA_API}/texts/<ref>",
        },
        "parameters": {
            "max_leaves": args.max_leaves,
            "timeout_seconds": args.timeout,
            "sleep_seconds": args.sleep,
            "progress_every": args.progress_every,
        },
        "stats": {
            "leaf_count_raw": len(all_leaves),
            "leaf_count_unique": total,
            "duplicate_titles_removed": duplicate_count,
            "keep_count": keep_count,
            "fix_count": len(fixes),
            "remove_count": len(removals),
            "elapsed_seconds": elapsed_seconds,
            "name_cache_size": len(name_cache),
            "probed_ref_count": len(probe_cache),
        },
        "fixes": fixes,
        "removals": removals,
    }

    if args.include_kept:
        report["kept"] = kept

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(
        report, indent=2, ensure_ascii=True), encoding="utf-8")

    print(f"[crawler] Report written to: {output_path}")
    print(
        f"[crawler] Summary -> keep={keep_count}, fix={len(fixes)}, remove={len(removals)}, elapsed={elapsed_seconds}s"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
