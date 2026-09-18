"""
One-off backfill: recompute historical ai_usage_log.cost_usd using the
current (post-Prompt-33a) backend.cost_meter._PRICE_PER_M table.

Background (plan.md §20.1-C1 / §20a.5): every row written before this fix
landed used a price table missing the production Gemini model
("gemini-3.5-flash-lite"), so any such row was inserted with cost_usd=0.0
regardless of actual token usage. Each row still carries its real
input_tokens/output_tokens, so an exact-for-currently-known-pricing
recompute is possible — this script does that recompute, not a guess.

NOT wired into any automated pipeline and NOT run as part of the Prompt 33a
pass that authored it — mutating financial ledger rows against the only
Supabase project this codebase has (no staging environment, see plan.md
§22's "solo developer, no staging environment" note) is exactly the kind of
action that needs a human at the keyboard reviewing --dry-run output first.

Usage:
    python3 scripts/recompute_ai_usage_log_cost.py                # dry run (default)
    python3 scripts/recompute_ai_usage_log_cost.py --execute       # actually writes

Requires SUPABASE_URL + SUPABASE_SECRET_KEY (service-role key — this must
bypass RLS to touch every user's rows, not just the caller's own).

After a real run, paste the printed pre/post totals into the commit message
per plan.md §20a.5's "record the pre/post totals in the commit message"
instruction.

CAUTION — same-day rows and the live budget ceiling: check_user_budget_and_
enforce() (cost_meter.py:285) sums TODAY's cost_usd per caller before every
/ask dispatch. If this script corrects rows from the CURRENT UTC day (not
just past days), any caller whose real spend today was already near or over
PER_USER_DAILY_BUDGET_USD -- invisible until now because those rows read
$0.00 -- will see their running total jump the moment this script commits,
and their very next request may be blocked without warning. Prefer running
this against past-day rows only during low-traffic hours, or accept that a
same-day correction can immediately trip the ceiling for someone already
over it.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.cost_meter import estimate_cost_usd  # noqa: E402

_PAGE_SIZE = 1000
_MAX_PAGES = 2000  # safety backstop, matches cost_meter.py's pagination cap pattern


def resolve_supabase_config() -> dict[str, str]:
    load_dotenv()
    url = (os.environ.get("SUPABASE_URL") or "").strip()
    secret_key = (os.environ.get("SUPABASE_SECRET_KEY") or "").strip()
    if not url:
        raise RuntimeError("Missing SUPABASE_URL.")
    if not secret_key:
        raise RuntimeError(
            "Missing SUPABASE_SECRET_KEY. The service-role key is required "
            "to update every row, not just the caller's own (RLS)."
        )
    return {"url": url, "secret_key": secret_key}


def fetch_all_rows(client: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    for _ in range(_MAX_PAGES):
        result = (
            client.table("ai_usage_log")
            .select("id,provider,model,input_tokens,output_tokens,cost_usd")
            .range(offset, offset + _PAGE_SIZE - 1)
            .execute()
        )
        page = result.data if isinstance(result.data, list) else []
        rows.extend(page)
        if len(page) < _PAGE_SIZE:
            return rows
        offset += _PAGE_SIZE
    print(
        f"WARNING: hit the {_MAX_PAGES}-page safety cap ({len(rows)} rows) — "
        "there may be more rows than this script fetched.",
        file=sys.stderr,
    )
    return rows


def build_updates(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], float, float]:
    """Returns (rows_needing_update, old_total, recomputed_total)."""
    updates = []
    old_total = 0.0
    new_total = 0.0
    for row in rows:
        old_cost = float(row.get("cost_usd") or 0.0)
        old_total += old_cost
        recomputed = estimate_cost_usd(
            str(row.get("model") or ""),
            int(row.get("input_tokens") or 0),
            int(row.get("output_tokens") or 0),
        )
        new_total += recomputed
        if round(recomputed, 8) != round(old_cost, 8):
            updates.append({"id": row["id"], "old_cost_usd": old_cost, "new_cost_usd": round(recomputed, 8)})
    return updates, old_total, new_total


def apply_updates(client: Any, updates: list[dict[str, Any]]) -> None:
    for update in updates:
        client.table("ai_usage_log").update(
            {"cost_usd": update["new_cost_usd"]}
        ).eq("id", update["id"]).execute()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true",
        help="Actually write the recomputed cost_usd values. Without this "
             "flag, the script only reports what it WOULD change.",
    )
    args = parser.parse_args()

    config = resolve_supabase_config()
    from supabase import create_client  # local import: only needed when this script actually runs
    client = create_client(config["url"], config["secret_key"])

    print("Fetching ai_usage_log rows...")
    rows = fetch_all_rows(client)
    print(f"Fetched {len(rows)} rows.")

    updates, old_total, new_total = build_updates(rows)

    print(f"Rows needing a cost_usd correction: {len(updates)} / {len(rows)}")
    print(f"Pre-fix  total cost_usd across all fetched rows: ${old_total:.6f}")
    print(f"Post-fix total cost_usd across all fetched rows: ${new_total:.6f}")
    print(f"Delta (recovered spend that was previously invisible): ${new_total - old_total:.6f}")

    if not updates:
        print("Nothing to update.")
        return

    if not args.execute:
        print("\nDRY RUN — no writes performed. Re-run with --execute to apply.")
        for sample in updates[:10]:
            print(f"  id={sample['id']} {sample['old_cost_usd']:.6f} -> {sample['new_cost_usd']:.6f}")
        if len(updates) > 10:
            print(f"  ... and {len(updates) - 10} more")
        return

    print(f"\nApplying {len(updates)} updates...")
    apply_updates(client, updates)
    print("Done. Paste the pre/post totals above into the commit message (plan.md §20a.5).")


if __name__ == "__main__":
    main()
