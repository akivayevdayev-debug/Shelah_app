#!/usr/bin/env python3
"""
Live acceptance check for the answer_feedback migration (plan.md §29.7).

Run this AFTER pasting scripts/migrate_answer_feedback.sql into the Supabase
SQL Editor for the real project. It verifies -- against the live database,
not by reading the SQL and assuming -- the two properties the feedback
feature depends on:

  1. anon/authenticated CAN insert (the widget must work for signed-out and
     signed-in readers alike).
  2. anon/authenticated CANNOT select at all -- the migration deliberately
     defines no SELECT policy so only the backend's secret-key client (via
     the devtools feedback-digest route) can read rows.

(2) is checked two ways:
  - Structurally, via the existing get_schema_snapshot() RPC (service-role
    only, plan.md §23.2.3): confirms live that zero SELECT policies exist on
    the table. Postgres denies a command to every non-bypassrls role when no
    policy grants it, so zero SELECT policies means both anon AND
    authenticated are denied -- this does not require minting an
    authenticated-role JWT to prove.
  - Empirically, with the anon-key client: insert a probe row, then try to
    read it back through the same anon client. A real deny means zero rows
    come back, not just "no error".

Usage:
  python3 scripts/verify_answer_feedback_migration.py
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from postgrest import ReturnMethod
from supabase import create_client

load_dotenv()

TABLE = (os.environ.get("SUPABASE_ANSWER_FEEDBACK_TABLE") or "answer_feedback").strip()


class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    RESET = "\033[0m"


def ok(msg: str) -> None:
    print(f"{Colors.GREEN}PASS{Colors.RESET}  {msg}")


def fail(msg: str) -> None:
    print(f"{Colors.RED}FAIL{Colors.RESET}  {msg}")


def info(msg: str) -> None:
    print(f"{Colors.YELLOW}INFO{Colors.RESET}  {msg}")


def _find_table_meta(snapshot):
    """The snapshot entry for TABLE, or None when the snapshot lacks it."""
    if not snapshot:
        return None
    for t in snapshot.get("tables", []):
        if t.get("table_name") == TABLE:
            return t
    return None


def _check_table_policies(table_meta) -> int:
    """RLS is on, no SELECT-capable policy, and an INSERT policy exists.
    Returns the number of failed checks."""
    failures = 0
    if table_meta.get("rls_enabled"):
        ok(f"{TABLE}.rls_enabled = true")
    else:
        fail(f"{TABLE}.rls_enabled = false -- RLS is OFF, every row would be world-readable")
        failures += 1

    policies = table_meta.get("policies", [])
    select_policies = [p for p in policies if p.get("cmd") in ("SELECT", "ALL")]
    insert_policies = [p for p in policies if p.get("cmd") in ("INSERT", "ALL")]

    if select_policies:
        fail(
            f"{TABLE} has SELECT-capable polic{'y' if len(select_policies) == 1 else 'ies'}: "
            f"{[p.get('name') for p in select_policies]} -- anon/authenticated should NOT be "
            f"able to read"
        )
        failures += 1
    else:
        ok(
            f"{TABLE} has zero SELECT policies -- Postgres RLS denies SELECT to every "
            f"non-bypassrls role (anon AND authenticated) when a command has no policy"
        )

    if insert_policies:
        ok(f"{TABLE} has an INSERT policy: {[p.get('name') for p in insert_policies]}")
    else:
        fail(f"{TABLE} has no INSERT policy -- POST /api/feedback will fail for anon/authenticated")
        failures += 1
    return failures


def _check_structure(service) -> int:
    """Structural check via get_schema_snapshot(). Returns the failure count."""
    print(f"\n=== Structural check: {TABLE} via get_schema_snapshot() ===")
    try:
        snapshot = service.rpc("get_schema_snapshot", {}).execute().data
    except Exception as e:
        fail(f"get_schema_snapshot() RPC call failed: {e}")
        info("Has scripts/sql/introspect_schema.sql been run (separate, pre-existing migration)?")
        return 1

    if snapshot is None:
        fail("get_schema_snapshot() returned null -- the structural check could not run")
        info("Has scripts/sql/introspect_schema.sql been run (separate, pre-existing migration)?")
        return 1

    table_meta = _find_table_meta(snapshot)
    if table_meta is None:
        fail(f"public.{TABLE} not found by get_schema_snapshot() -- migration not applied yet?")
        return 1
    return _check_table_policies(table_meta)


def _check_anon_select_denied(anon, probe_hash: str) -> int:
    """The anon client must not be able to read back the row it just inserted."""
    try:
        result = anon.table(TABLE).select("*").eq("question_hash", probe_hash).execute()
        rows = result.data or []
        if rows:
            fail(
                f"anon SELECT returned {len(rows)} row(s) for a row it just inserted -- "
                f"RLS is NOT blocking SELECT for anon"
            )
            return 1
        ok("anon SELECT of its own just-inserted row returned zero rows -- RLS blocks anon SELECT")
    except Exception as e:
        # PostgREST can surface a denied SELECT as an error instead of an
        # empty result depending on grants; either shape is an acceptable
        # deny as long as no real data comes back.
        ok(f"anon SELECT raised instead of returning data (also an acceptable deny): {e}")
    return 0


def _check_anon_role(anon, service) -> int:
    """Empirical check: insert a probe row as anon, try to read it back,
    then clean it up with the service-role client. Returns the failure count."""
    print("\n=== Empirical check: anon-role client (publishable key, no bearer JWT) ===")
    probe_hash = f"acceptance-test-{uuid.uuid4().hex}"
    probe_row = {
        "user_id": None,
        "question_hash": probe_hash,
        "verdict": "helpful",
        "comment": "acceptance test row -- safe to delete",
        "mode": "balanced",
        "language": "en",
        "fallback": False,
        "safety_class": "ok",
    }

    failures = 0
    try:
        # returning=minimal: supabase-py defaults to representation, i.e.
        # INSERT ... RETURNING, which Postgres only allows for a role that also
        # holds SELECT. migrate_security_hardening.sql deliberately revokes
        # SELECT from anon, so without this a correctly secured table fails
        # this probe with 42501 "permission denied for table".
        anon.table(TABLE).insert(probe_row, returning=ReturnMethod.minimal).execute()
        ok("anon INSERT succeeded (matches anyone_can_submit_feedback policy)")
    except Exception as e:
        fail(f"anon INSERT failed: {e}")
        failures += 1

    failures += _check_anon_select_denied(anon, probe_hash)

    try:
        service.table(TABLE).delete().eq("question_hash", probe_hash).execute()
        info("cleaned up acceptance-test row via service-role client")
    except Exception as e:
        info(f"cleanup delete failed (non-fatal, row is inert test data): {e}")
    return failures


def main() -> int:
    url = (os.environ.get("SUPABASE_URL") or "").strip()
    secret_key = (os.environ.get("SUPABASE_SECRET_KEY") or "").strip()
    anon_key = (os.environ.get("SUPABASE_PUBLISHABLE_KEY") or "").strip()

    if not url or not secret_key or not anon_key:
        fail("SUPABASE_URL / SUPABASE_SECRET_KEY / SUPABASE_PUBLISHABLE_KEY must all be set in .env")
        return 1

    service = create_client(url, secret_key)
    anon = create_client(url, anon_key)

    failures = _check_structure(service)
    failures += _check_anon_role(anon, service)

    print()
    if failures:
        fail(f"{failures} check(s) failed")
        return 1
    ok("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
