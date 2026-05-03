#!/usr/bin/env python3
"""
Migrate customs JSON files into Supabase community_knowledge with deterministic upserts.

Usage:
  python3 scripts/migrate_customs_to_supabase.py
  python3 scripts/migrate_customs_to_supabase.py --dry-run
  python3 scripts/migrate_customs_to_supabase.py --community Ashkenazi
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

try:
    from supabase import create_client
except Exception as exc:  # pragma: no cover
    raise RuntimeError(
        "supabase package is required. Install requirements first.") from exc

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CUSTOMS_DIR = PROJECT_ROOT / "customs"


def _normalize_text(value: Any, max_chars: int = 2000) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) > max_chars:
        text = f"{text[:max_chars].rstrip()}..."
    return text


def _stable_id(community_name: str, topic: str, halakhic_source: str) -> str:
    key = f"{community_name.lower().strip()}|{topic.lower().strip()}|{halakhic_source.lower().strip()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _authorities_fallback(payload: Dict[str, Any]) -> str:
    authorities = payload.get("core_halachic_authorities")
    if not isinstance(authorities, dict):
        return ""

    candidates: List[str] = []
    for field in (
        "primary_codes",
        "major_rishonim_base",
        "later_ashkenazi_poskim",
        "later_sephardi_poskim",
        "later_moroccan_poskim",
        "later_turkish_poskim",
    ):
        values = authorities.get(field)
        if isinstance(values, list):
            candidates.extend(str(v).strip() for v in values if str(v).strip())

    deduped: List[str] = []
    seen = set()
    for item in candidates:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    return ", ".join(deduped[:4])


def _build_content(summary: str, common_practices: Any, notes: Any) -> str:
    parts: List[str] = []
    summary = _normalize_text(summary)
    if summary:
        parts.append(summary)

    if isinstance(common_practices, list) and common_practices:
        trimmed = [
            _normalize_text(item, max_chars=180)
            for item in common_practices
            if _normalize_text(item, max_chars=180)
        ]
        if trimmed:
            parts.append(f"Common practices: {' | '.join(trimmed[:4])}")

    notes_text = _normalize_text(notes)
    if notes_text:
        parts.append(f"Notes: {notes_text}")

    return _normalize_text("\n".join(parts), max_chars=2200)


def _parse_modern_payload(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    community_name = _normalize_text(payload.get(
        "name") or payload.get("heritage_id") or "Unknown")
    fallback_source = _authorities_fallback(
        payload) or "Community halakhic tradition"

    rows: List[Dict[str, str]] = []
    for item in payload.get("halacha_index", []) or []:
        if not isinstance(item, dict):
            continue

        topic = _normalize_text(
            item.get("topic") or item.get("index") or "General")
        halakhic_source = _normalize_text(
            item.get("source") or fallback_source or "Community tradition")
        content = _build_content(
            summary=item.get("summary"),
            common_practices=item.get("common_practices"),
            notes=item.get("notes"),
        )

        if not content:
            continue

        rows.append({
            "id": _stable_id(community_name, topic, halakhic_source),
            "community_name": community_name,
            "topic": topic,
            "halakhic_source": halakhic_source,
            "content": content,
        })

    return rows


def _parse_legacy_payload(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for community_name, topics in payload.items():
        if not isinstance(topics, dict):
            continue

        community_name_text = _normalize_text(community_name)
        for topic_key, topic_data in topics.items():
            if not isinstance(topic_data, dict):
                continue

            topic = _normalize_text(topic_key.replace("_", " "))
            halakhic_source = _normalize_text(
                topic_data.get("source") or "Community tradition")
            content = _build_content(
                summary=topic_data.get("ruling"),
                common_practices=topic_data.get("keywords"),
                notes=topic_data.get("notes"),
            )

            if not content:
                continue

            rows.append({
                "id": _stable_id(community_name_text, topic, halakhic_source),
                "community_name": community_name_text,
                "topic": topic,
                "halakhic_source": halakhic_source,
                "content": content,
            })

    return rows


def load_rows_from_json(customs_dir: Path, community_filter: str = "") -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []

    for path in sorted(customs_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[WARN] Skipping {path.name}: {exc}")
            continue

        parsed_rows: List[Dict[str, str]] = []
        if isinstance(payload, dict) and "halacha_index" in payload:
            parsed_rows = _parse_modern_payload(payload)
        elif isinstance(payload, dict):
            parsed_rows = _parse_legacy_payload(payload)

        if community_filter:
            community_key = community_filter.strip().lower()
            parsed_rows = [
                row for row in parsed_rows
                if row.get("community_name", "").strip().lower() == community_key
            ]

        rows.extend(parsed_rows)

    deduped: Dict[str, Dict[str, str]] = {}
    for row in rows:
        deduped[row["id"]] = row

    return list(deduped.values())


def chunked(items: List[Dict[str, str]], size: int) -> List[List[Dict[str, str]]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def resolve_supabase_config() -> Dict[str, str]:
    load_dotenv()

    url = (os.environ.get("SUPABASE_URL") or os.environ.get(
        "NEXT_PUBLIC_SUPABASE_URL") or "").strip()
    service_role_key = (os.environ.get(
        "SUPABASE_SERVICE_ROLE_KEY") or "").strip()

    if not url:
        raise RuntimeError(
            "Missing SUPABASE_URL (or NEXT_PUBLIC_SUPABASE_URL).")
    if not service_role_key:
        raise RuntimeError(
            "Missing SUPABASE_SERVICE_ROLE_KEY. Service role key is required for migration upserts.")

    return {
        "url": url,
        "service_role_key": service_role_key,
    }


def run_migration(table_name: str, rows: List[Dict[str, str]], dry_run: bool, chunk_size: int) -> None:
    print(f"Prepared {len(rows)} rows for upsert into '{table_name}'.")
    if not rows:
        print("Nothing to migrate.")
        return

    if dry_run:
        print("Dry run enabled. Sample rows:")
        for sample in rows[:3]:
            print(json.dumps(sample, ensure_ascii=False, indent=2))
        return

    cfg = resolve_supabase_config()
    client = create_client(cfg["url"], cfg["service_role_key"])

    # Fast sanity probe to fail early when table is missing.
    client.table(table_name).select("id").limit(1).execute()

    batches = chunked(rows, max(1, chunk_size))
    total = 0
    for index, batch in enumerate(batches, start=1):
        client.table(table_name).upsert(batch, on_conflict="id").execute()
        total += len(batch)
        print(f"Upserted batch {index}/{len(batches)} ({len(batch)} rows)")

    print(f"Done. Upserted {total} rows into '{table_name}'.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upsert customs JSON into Supabase community_knowledge table.")
    parser.add_argument("--table", default=os.environ.get("SUPABASE_COMMUNITY_KNOWLEDGE_TABLE",
                        "community_knowledge"), help="Destination Supabase table name")
    parser.add_argument("--community", default="",
                        help="Optional exact community_name filter")
    parser.add_argument("--chunk-size", type=int,
                        default=250, help="Upsert batch size")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse JSON and print sample payloads without writing")
    args = parser.parse_args()

    rows = load_rows_from_json(CUSTOMS_DIR, community_filter=args.community)
    run_migration(args.table, rows, dry_run=args.dry_run,
                  chunk_size=args.chunk_size)


if __name__ == "__main__":
    main()
