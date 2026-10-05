#!/usr/bin/env python3
"""
Migrate customs JSON files into Supabase community_knowledge with deterministic upserts.

The JSON files in customs/ are the source of truth; community_knowledge is the
serving copy /ask retrieves from. Each row's community_name is the file's
runtime.lens_key (the canonical key backend/rag.py matches with ilike), falling
back to its `name`.

Usage:
  python3 scripts/migrate_customs_to_supabase.py --dry-run
  python3 scripts/migrate_customs_to_supabase.py                     # upsert only
  python3 scripts/migrate_customs_to_supabase.py --prune             # upsert, then delete stale rows
  python3 scripts/migrate_customs_to_supabase.py --community Ashkenaz
  python3 scripts/migrate_customs_to_supabase.py --emit-sql DIR      # no network: one SQL file per community

Row ids hash community|topic|source, so a topic or source that is renamed gets a
new id and strands the old row. --prune (or the delete in each emitted SQL file)
removes those, plus the rows of the retired customs_db.json aggregate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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

# Not communities: the JSON Schema, and the retired flat aggregate (backend/customs.py
# skips it too). Its rows are removed by --prune via LEGACY_COMMUNITY_NAMES.
SKIPPED_FILES = {"schema.json", "customs_db.json"}
LEGACY_COMMUNITY_NAMES = ("Kafkazi", "Sephardic", "Ashkenazic", "Bukharian")

MAX_PRACTICES = 6
MAX_VARIANTS = 4
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Caveats the AI must carry into an answer. "well-attested" needs none. Each
# one ends by sending the user to their own rabbi: rabbis rule differently.
_CONFIDENCE_NOTES = {
    "disputed": "Confidence: disputed. Rabbis differ; give each side and tell the user to ask their own rabbi.",
    "regional": "Confidence: regional. Only some places or subgroups; the user's own rabbi decides which applies.",
    "needs-review": "Confidence: needs review. Thinly sourced; hedge and tell the user to ask their own rabbi.",
}


def _truncate(text: str, max_chars: int) -> str:
    if len(text) > max_chars:
        return f"{text[:max_chars].rstrip()}..."
    return text


def _normalize_text(value: Any, max_chars: int = 2000) -> str:
    return _truncate(" ".join(str(value or "").strip().split()), max_chars)


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
        "later_poskim",
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


def _variant_lines(variants: Any) -> List[str]:
    lines: List[str] = []
    for variant in variants if isinstance(variants, list) else []:
        if not isinstance(variant, dict):
            continue
        practice = _normalize_text(variant.get("practice"), max_chars=240)
        if not practice:
            continue
        subgroup = _normalize_text(variant.get("subgroup"), max_chars=60)
        lines.append(f"{subgroup}: {practice}" if subgroup else practice)
    return lines[:MAX_VARIANTS]


def _build_content(summary: str, common_practices: Any, notes: Any,
                   variants: Any = None, confidence: Any = None) -> str:
    parts: List[str] = []
    summary = _normalize_text(summary)
    if summary:
        parts.append(summary)

    # Straight after the summary, so the prompt's per-row cap can never cut it off.
    confidence_note = _CONFIDENCE_NOTES.get(str(confidence or "").strip().lower())
    if confidence_note:
        parts.append(confidence_note)

    variant_lines = _variant_lines(variants)
    if variant_lines:
        parts.append(f"Variants: {' | '.join(variant_lines)}")

    if isinstance(common_practices, list) and common_practices:
        trimmed = [
            _normalize_text(item, max_chars=220)
            for item in common_practices
            if _normalize_text(item, max_chars=220)
        ]
        if trimmed:
            parts.append(f"Common practices: {' | '.join(trimmed[:MAX_PRACTICES])}")

    notes_text = _normalize_text(notes)
    if notes_text:
        parts.append(f"Notes: {notes_text}")

    # Join with "\n" and cap the length WITHOUT _normalize_text(), which would
    # collapse those line breaks back into spaces. Each part was already
    # normalised above, so only the separators between parts are newlines.
    return _truncate("\n".join(parts), 2200)


def _community_key(payload: Dict[str, Any]) -> str:
    """community_name for a file's rows: its runtime.lens_key, else its name."""
    runtime = payload.get("runtime")
    lens_key = runtime.get("lens_key") if isinstance(runtime, dict) else None
    return _normalize_text(lens_key or payload.get("name") or payload.get("heritage_id") or "Unknown")


def _parse_unique_minhagim(payload: Dict[str, Any], community_name: str) -> List[Dict[str, str]]:
    """Rows for the 3.0 list of distinctive customs; the legacy {examples} dict has none."""
    unique = payload.get("unique_minhagim")
    rows: List[Dict[str, str]] = []
    for item in unique if isinstance(unique, list) else []:
        if not isinstance(item, dict):
            continue
        name = _normalize_text(item.get("name"), max_chars=160)
        description = _normalize_text(item.get("description"))
        if not name or not description:
            continue
        when = _normalize_text(item.get("when"), max_chars=200)
        topic = f"Distinctive custom: {name}"
        halakhic_source = _normalize_text(item.get("source") or "Community tradition")
        content = _build_content(
            summary=description,
            common_practices=[f"When: {when}"] if when else None,
            notes=None,
            confidence=item.get("confidence"),
        )
        rows.append({
            "id": _stable_id(community_name, topic, halakhic_source),
            "community_name": community_name,
            "topic": topic,
            "halakhic_source": halakhic_source,
            "content": content,
        })
    return rows


def _parse_modern_payload(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    community_name = _community_key(payload)
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
            variants=item.get("variants"),
            confidence=item.get("confidence"),
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

    rows.extend(_parse_unique_minhagim(payload, community_name))
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
        if path.name.lower() in SKIPPED_FILES:
            continue
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

    url = (os.environ.get("SUPABASE_URL") or "").strip()
    secret_key = (os.environ.get("SUPABASE_SECRET_KEY") or "").strip()

    if not url:
        raise RuntimeError("Missing SUPABASE_URL.")
    if not secret_key:
        raise RuntimeError(
            "Missing SUPABASE_SECRET_KEY. The secret key is required for migration upserts.")

    return {
        "url": url,
        "secret_key": secret_key,
    }


def known_community_names(customs_dir: Path, community_filter: str = "") -> List[str]:
    """Every community_name this corpus has used for the current files: each file's
    lens_key, its `name` (what earlier runs wrote), and the retired aggregate's
    names. These are the only names --prune and --emit-sql ever delete from."""
    names: List[str] = []
    for path in sorted(customs_dir.glob("*.json")):
        if path.name.lower() in SKIPPED_FILES:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict) or "halacha_index" not in payload:
            continue
        key = _community_key(payload)
        if community_filter and key.lower() != community_filter.strip().lower():
            continue
        names.extend([key, _normalize_text(payload.get("name"))])
    if not community_filter:
        names.extend(LEGACY_COMMUNITY_NAMES)
    return sorted({n for n in names if n})


def _sql_text(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def build_sql(table_name: str, rows: List[Dict[str, str]], delete_names: List[str]) -> str:
    """One transaction: drop the corpus's old rows for these communities, then insert
    the current ones. Paste into the Supabase SQL Editor (runs as the owner, so RLS
    does not apply)."""
    if not _SAFE_IDENTIFIER.match(table_name):
        raise ValueError(f"Unsafe table name: {table_name!r}")
    lines = ["begin;"]
    if delete_names:
        names = ", ".join(_sql_text(n) for n in delete_names)
        lines.append(f"delete from public.{table_name} where community_name in ({names});")
    if rows:
        values = ",\n".join(
            "  (" + ", ".join(_sql_text(row[k]) for k in (
                "id", "community_name", "topic", "halakhic_source", "content")) + ")"
            for row in rows
        )
        lines.append(
            f"insert into public.{table_name} (id, community_name, topic, halakhic_source, content) values\n"
            f"{values}\n"
            "on conflict (id) do update set community_name = excluded.community_name, "
            "topic = excluded.topic, halakhic_source = excluded.halakhic_source, content = excluded.content;"
        )
    lines.append("commit;")
    return "\n".join(lines) + "\n"


def emit_sql(customs_dir: Path, table_name: str, out_dir: Path, community_filter: str = "") -> List[Path]:
    """Write one SQL file per community (plus one for the retired aggregate's rows),
    each small enough to paste into the SQL Editor."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = load_rows_from_json(customs_dir, community_filter=community_filter)
    by_community: Dict[str, List[Dict[str, str]]] = {}
    for row in rows:
        by_community.setdefault(row["community_name"], []).append(row)

    written: List[Path] = []
    for index, (community, community_rows) in enumerate(sorted(by_community.items()), start=1):
        delete_names = [n for n in known_community_names(customs_dir, community)
                        if n not in LEGACY_COMMUNITY_NAMES or n == community]
        path = out_dir / f"{index:02d}_{re.sub(r'[^A-Za-z0-9]+', '_', community).strip('_').lower()}.sql"
        path.write_text(build_sql(table_name, community_rows, delete_names), encoding="utf-8")
        written.append(path)

    if not community_filter:
        legacy = [n for n in LEGACY_COMMUNITY_NAMES if n not in by_community]
        path = out_dir / "00_retired_customs_db_cleanup.sql"
        path.write_text(build_sql(table_name, [], legacy), encoding="utf-8")
        written.insert(0, path)
    return written


def prune_stale_rows(client: Any, table_name: str, rows: List[Dict[str, str]],
                     names: List[str], page_size: int = 1000) -> int:
    """Delete rows under `names` whose id is not in `rows`. Never touches any other
    community_name. Returns the number deleted."""
    keep = {row["id"] for row in rows}
    deleted = 0
    for name in names:
        existing: List[str] = []
        start = 0
        while True:
            page = (client.table(table_name).select("id").eq("community_name", name)
                    .range(start, start + page_size - 1).execute().data or [])
            existing.extend(str(r["id"]) for r in page)
            if len(page) < page_size:
                break
            start += page_size
        stale = [i for i in existing if i not in keep]
        for batch in (stale[i:i + 200] for i in range(0, len(stale), 200)):
            client.table(table_name).delete().in_("id", batch).execute()
        if stale:
            print(f"Pruned {len(stale)} stale row(s) under '{name}'.")
        deleted += len(stale)
    return deleted


def run_migration(table_name: str, rows: List[Dict[str, str]], dry_run: bool, chunk_size: int,
                  prune_names: List[str] | None = None) -> None:
    print(f"Prepared {len(rows)} rows for upsert into '{table_name}'.")
    if not rows:
        print("Nothing to migrate.")
        return

    if dry_run:
        print("Dry run enabled. Sample rows:")
        for sample in rows[:3]:
            print(json.dumps(sample, ensure_ascii=False, indent=2))
        if prune_names:
            print(f"--prune would clear stale rows under: {', '.join(prune_names)}")
        return

    cfg = resolve_supabase_config()
    client = create_client(cfg["url"], cfg["secret_key"])

    # Fast sanity probe to fail early when table is missing.
    client.table(table_name).select("id").limit(1).execute()

    batches = chunked(rows, max(1, chunk_size))
    total = 0
    for index, batch in enumerate(batches, start=1):
        client.table(table_name).upsert(batch, on_conflict="id").execute()
        total += len(batch)
        print(f"Upserted batch {index}/{len(batches)} ({len(batch)} rows)")

    print(f"Done. Upserted {total} rows into '{table_name}'.")

    # Prune only after every upsert succeeded, so a failed run never leaves a
    # community with fewer rows than it started with.
    if prune_names:
        removed = prune_stale_rows(client, table_name, rows, prune_names)
        print(f"Prune complete. Removed {removed} stale row(s).")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upsert customs JSON into Supabase community_knowledge table.")
    parser.add_argument("--table", default=os.environ.get("SUPABASE_COMMUNITY_KNOWLEDGE_TABLE",
                        "community_knowledge"), help="Destination Supabase table name")
    parser.add_argument("--community", default="",
                        help="Optional exact community_name (lens key) filter")
    parser.add_argument("--chunk-size", type=int,
                        default=250, help="Upsert batch size")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse JSON and print sample payloads without writing")
    parser.add_argument("--prune", action="store_true",
                        help="After upserting, delete this corpus's rows that no longer exist in the JSON")
    parser.add_argument("--emit-sql", metavar="DIR", default="",
                        help="Write one SQL file per community to DIR instead of connecting to Supabase")
    args = parser.parse_args()

    if args.emit_sql:
        for path in emit_sql(CUSTOMS_DIR, args.table, Path(args.emit_sql), community_filter=args.community):
            print(f"Wrote {path}")
        return

    rows = load_rows_from_json(CUSTOMS_DIR, community_filter=args.community)
    prune_names = known_community_names(CUSTOMS_DIR, args.community) if args.prune else None
    run_migration(args.table, rows, dry_run=args.dry_run,
                  chunk_size=args.chunk_size, prune_names=prune_names)


if __name__ == "__main__":
    main()
