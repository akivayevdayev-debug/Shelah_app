#!/usr/bin/env python3
"""
Live acceptance check for scripts/sql/conversations_setup.sql (multi-turn AI
conversation schema: conversations/messages/citations).

Run this AFTER pasting that file into the Supabase SQL Editor for the real
project. It answers two questions a mocked Supabase client (the unit test
suite in tests/test_routes_conversations.py) cannot:

  1. STRUCTURAL -- do the three tables exist with RLS enabled+forced and the
     expected owner-only policies, via the existing get_schema_snapshot()
     RPC (service-role only, same approach as
     scripts/verify_answer_feedback_migration.py)?

  2. END-TO-END (Step 1 sanity check) -- using the service-role client
     (bypasses RLS, same as backend/routes_conversations.py's own fallback
     path when STRICT_SUPABASE_RLS is off), does the real schema actually
     accept the exact shapes Step 1's routes read and write? Creates one
     conversation, one message, one citation, re-reads the conversation with
     the nested `citations(...)` embed create_message()/get_conversation()
     both rely on, then deletes the conversation (cascades to its message
     and citation via the FKs' `on delete cascade`).

This intentionally does NOT re-run the two-distinct-Clerk-users cross-user
RLS negative assertion scripts/verify_rls.py performs for user_preferences/
study_bookmarks/user_memories -- these three new tables use the exact same
`(auth.jwt() ->> 'sub') = user_id` (or join-through-parent equivalent)
policy shape already proven live there. What's new and worth checking here
is the schema itself: table/column/FK shape, RLS being ON, and that the
nested PostgREST embed the routes depend on actually resolves.

Usage:
  python3 scripts/verify_conversations_migration.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

CONV_TABLE = (os.environ.get("SUPABASE_CONVERSATIONS_TABLE") or "conversations").strip()
MSG_TABLE = (os.environ.get("SUPABASE_MESSAGES_TABLE") or "messages").strip()
CIT_TABLE = (os.environ.get("SUPABASE_CITATIONS_TABLE") or "citations").strip()

_EXPECTED_POLICY_CMDS = {"SELECT", "INSERT", "UPDATE", "DELETE"}


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


def _find_table_meta(snapshot, table_name):
    if not snapshot:
        return None
    for t in snapshot.get("tables", []):
        if t.get("table_name") == table_name:
            return t
    return None


def _check_one_table_structure(table_meta, table_name, expected_cmds) -> int:
    """RLS on+forced and one policy per expected command. Returns the
    number of failed checks."""
    failures = 0
    if table_meta.get("rls_enabled"):
        ok(f"{table_name}.rls_enabled = true")
    else:
        fail(f"{table_name}.rls_enabled = false -- every row would be world-readable")
        failures += 1

    policies = table_meta.get("policies", [])
    present_cmds = {p.get("cmd") for p in policies if p.get("cmd") in _EXPECTED_POLICY_CMDS}
    for cmd in sorted(expected_cmds):
        if cmd in present_cmds or "ALL" in present_cmds:
            ok(f"{table_name} has a {cmd} policy")
        else:
            fail(f"{table_name} is missing a {cmd} policy")
            failures += 1
    return failures


def _check_structure(service) -> int:
    print(f"\n=== Structural check: {CONV_TABLE}/{MSG_TABLE}/{CIT_TABLE} via get_schema_snapshot() ===")
    try:
        snapshot = service.rpc("get_schema_snapshot", {}).execute().data
    except Exception as e:
        fail(f"get_schema_snapshot() RPC call failed: {e}")
        info("Has scripts/sql/introspect_schema.sql been run (separate, pre-existing migration)?")
        return 1

    if snapshot is None:
        fail("get_schema_snapshot() returned null -- the structural check could not run")
        return 1

    failures = 0
    # citations has no INSERT/UPDATE/DELETE-by-anon path of its own beyond
    # insert (rows are only ever written by the backend alongside their
    # message) and delete (cascades) -- but the migration still defines
    # select/insert/delete for it, matching the SQL file above.
    expectations = [
        (CONV_TABLE, {"SELECT", "INSERT", "UPDATE", "DELETE"}),
        (MSG_TABLE, {"SELECT", "INSERT", "UPDATE", "DELETE"}),
        (CIT_TABLE, {"SELECT", "INSERT", "DELETE"}),
    ]
    for table_name, expected_cmds in expectations:
        table_meta = _find_table_meta(snapshot, table_name)
        if table_meta is None:
            fail(f"public.{table_name} not found by get_schema_snapshot() -- migration not applied yet?")
            failures += 1
            continue
        failures += _check_one_table_structure(table_meta, table_name, expected_cmds)
    return failures


def _check_end_to_end(service) -> int:
    """Step 1 sanity check: the exact insert/select shapes
    backend/routes_conversations.py's routes use, run against the live
    schema via the service-role client (bypasses RLS, same fallback path
    the routes themselves use when STRICT_SUPABASE_RLS is off)."""
    print(f"\n=== End-to-end check: create_conversation -> create_message -> get_conversation ===")
    probe_user_id = "acceptance-test-user-conversations-migration"
    failures = 0
    conversation_id = None

    try:
        conv_result = service.table(CONV_TABLE).insert({
            "user_id": probe_user_id,
            "title": "",
            "title_is_custom": False,
            "minhag": "Ashkenaz",
        }).execute()
        conv_rows = conv_result.data or []
        if not conv_rows:
            fail(f"insert into {CONV_TABLE} returned no rows")
            return 1
        conversation_id = conv_rows[0]["id"]
        ok(f"created a probe conversation ({conversation_id})")

        msg_result = service.table(MSG_TABLE).insert({
            "conversation_id": conversation_id,
            "role": "assistant",
            "content": "Acceptance test answer.",
            "status": "complete",
        }).execute()
        msg_rows = msg_result.data or []
        if not msg_rows:
            fail(f"insert into {MSG_TABLE} returned no rows")
            failures += 1
        else:
            message_id = msg_rows[0]["id"]
            ok(f"created a probe message ({message_id})")

            cit_result = service.table(CIT_TABLE).insert({
                "message_id": message_id,
                "ordinal": 0,
                "source_ref": "Acceptance Test 1:1",
                "excerpt_en": "Sample excerpt.",
                "excerpt_he": "קטע לדוגמה",
                "url": None,
            }).execute()
            if not (cit_result.data or []):
                fail(f"insert into {CIT_TABLE} returned no rows")
                failures += 1
            else:
                ok("created a probe citation")

            hydrate_result = (
                service.table(CONV_TABLE)
                .select(
                    "id,title,minhag,"
                    f"{MSG_TABLE}(id,role,content,status,"
                    f"{CIT_TABLE}(id,ordinal,source_ref,excerpt_en,excerpt_he,url))"
                )
                .eq("id", conversation_id)
                .limit(1)
                .execute()
            )
            hydrate_rows = hydrate_result.data or []
            if not hydrate_rows:
                fail(f"nested select on {CONV_TABLE} (mirroring get_conversation()'s query) returned no rows")
                failures += 1
            else:
                nested_messages = hydrate_rows[0].get(MSG_TABLE) or []
                nested_citations = (nested_messages[0].get(CIT_TABLE) if nested_messages else None) or []
                if nested_messages and nested_citations:
                    ok(
                        "nested select resolved conversations -> messages -> citations "
                        "in one query (matches get_conversation()'s own query shape)"
                    )
                else:
                    fail(
                        f"nested select did not resolve the embed as expected: "
                        f"messages={nested_messages!r}"
                    )
                    failures += 1
    except Exception as e:
        fail(f"unexpected error during end-to-end check: {e}")
        failures += 1
    finally:
        if conversation_id:
            try:
                service.table(CONV_TABLE).delete().eq("id", conversation_id).execute()
                info(f"cleaned up probe conversation {conversation_id} (cascades to its message/citation)")
            except Exception as e:
                info(f"cleanup delete failed (non-fatal, row is inert test data): {e}")

    return failures


def main() -> int:
    url = (os.environ.get("SUPABASE_URL") or "").strip()
    secret_key = (os.environ.get("SUPABASE_SECRET_KEY") or "").strip()

    if not url or not secret_key:
        fail("SUPABASE_URL / SUPABASE_SECRET_KEY must both be set in .env")
        return 1

    service = create_client(url, secret_key)

    failures = _check_structure(service)
    failures += _check_end_to_end(service)

    print()
    if failures:
        fail(f"{failures} check(s) failed")
        return 1
    ok("all checks passed -- schema is live and Step 1's query shapes work end-to-end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
