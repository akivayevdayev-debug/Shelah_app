#!/usr/bin/env python3
"""
Live Postgres RLS acceptance check (plan.md §21.2.2 STEP 2/3, §21.3 exit
criteria; claude_code_prompts.md Prompt 34).

This is NOT a unit test. It performs real HTTP calls against a deployed
Sh'elah instance and a real Supabase project, as two real (dedicated test)
signed-in users, to answer the one question a mocked Supabase client can
never answer: does auth.uid() actually resolve for a Clerk JWT in THIS
project, and does that make RLS actually gate access?

Design notes -- read before changing the assertions below:

  * Two layers, not one:

    1. DIRECT POSTGREST CHECKS (the real answer) -- for each of the three
       RLS-relevant tables (user_preferences, study_bookmarks,
       user_memories), insert a sentinel as test user A directly against
       Supabase's REST layer (bypassing the Flask app entirely), then:
         (a) confirm A can read it back (positive: RLS lets an owner see
             their own row),
         (b) confirm test user B's token, querying the SAME table filtered
             explicitly to A's user_id, gets ZERO rows back (negative: RLS
             actually blocks a non-owner -- this is "the teeth", per the
             prompt this script implements).
       This is done directly against PostgREST, not through
       /api/user/preferences or /api/bookmarks/semantic, on purpose: both
       of those routes already hard-filter every query to
       `.eq("user_id", <the CALLER's own id from their own JWT>)` at the
       application layer (backend/routes_user.py). Calling them as user B
       can therefore never surface user A's row no matter what RLS does --
       that would make a negative assertion routed through the app
       trivially true and prove nothing about RLS itself. Querying
       PostgREST directly, with user B's own forwarded JWT but a filter
       naming user A's user_id, is the only way to ask Postgres itself
       "does RLS let user B see user A's row" -- the actual question
       plan.md §21.2.2 STEP 3 needs answered.

    2. APP-ROUTED SMOKE CHECK (proves the full plumbing, not just the
       database) -- a PUT/GET or POST/GET sentinel round trip through the
       real deployed HTTP API for the two tables that have one
       (/api/user/preferences, /api/bookmarks/semantic), as user A only.
       This is what actually exercises Clerk JWT -> Flask ->
       _get_user_scoped_supabase_client() -> Supabase, the real request
       path plan.md §21.1's four call sites use. If Supabase isn't
       configured to trust Clerk (auth.uid() resolves NULL), this check
       fails exactly the way §21.1 warns about: not an error, not a 403,
       an empty/missing sentinel that looks like "no data yet".

  * user_memories has no dedicated HTTP endpoint (backend/rag.py's
    _fetch_user_memory_summaries/_store_user_memory_summary are only
    called internally by /ask, which would mean a real, non-deterministic,
    non-free AI call just to exercise a memory write) -- so it only gets
    layer 1 (direct PostgREST) here. Layer 1 alone still answers the RLS
    question for that table; live observed-vs-service-role counts for it
    are additionally surfaced on every real authenticated request via
    GET /api/devtools/rls-audit (backend/routes_devtools.py), so the "no
    user-visible symptom" gap plan.md §21.1 flagged for this table has a
    second, always-on check, not just this on-demand script.

  * Test users must be DEDICATED test accounts, never real ones. This
    script upserts a sentinel into user_preferences (single row per
    user_id -- this OVERWRITES whatever was there), and inserts+deletes
    its own rows in study_bookmarks/user_memories (safe: those use their
    own `id`, not user_id, as the primary key, so cleanup only removes the
    rows this run created).

  * This script never creates a Clerk account or session. It only mints a
    fresh JWT from an EXISTING session_id via Clerk's Backend API
    (POST /sessions/{id}/tokens), or accepts an already-minted token
    directly. Both must be provisioned by the operator ahead of time.

Required environment (fails fast with a clear message if missing, not a
stack trace):
  RLS_VERIFY_BASE_URL   Deployed Sh'elah base URL (or DEPLOYED_URL/VERCEL_URL)
  SUPABASE_URL          Same project the deployed app points at
  SUPABASE_PUBLISHABLE_KEY

Test user A and B, EACH needs one of:
  RLS_TEST_USER_A_TOKEN         A pre-minted Clerk session JWT (quick local runs)
  RLS_TEST_USER_A_SESSION_ID    + CLERK_SECRET_KEY, to mint a fresh one (CI)
  (same pattern for RLS_TEST_USER_B_*)

Optional:
  VERCEL_AUTOMATION_BYPASS_SECRET   Bypasses Vercel Deployment Protection
    (Project Settings -> Deployment Protection -> Protection Bypass for
    Automation) if the deployed URL has that enabled. Does NOT bypass
    Vercel's Firewall-level checks (Attack Mode, managed bot/DDoS rulesets,
    custom WAF rules) -- Vercel's own docs say those "cannot be bypassed
    even with a valid bypass token." If Layer 2 still 429s with a Vercel
    "Security Checkpoint" page after this is set, check Project -> Firewall
    -> Firewall Observability for which rule actually matched. Confirmed
    live 2026-08-31 -- see plan.md §21's Prompt 34 update.

A session_id is not a permanent credential -- Clerk sessions expire (or end
via sign-out / Clerk's own session policy). Once a session has ended, minting
a token from its id fails with a 404 `resource_not_found` from
https://api.clerk.com/v1/sessions/{id}/tokens (confirmed live 2026-08-31,
see plan.md §21's Prompt 34 update) -- NOT an auth-header or Content-Type
problem, and not something this script can recover from on its own. If you
hit that, sign in again as the dedicated test user, copy the NEW session_id
from Clerk Dashboard -> Users -> (test user) -> Sessions, and update the
RLS_TEST_USER_<X>_SESSION_ID secret. Expect to do this periodically for a
scheduled/unattended run.

Exit code 0 only if every check ran and passed. Exit code 1 if any check
failed, or if required configuration is missing (fail closed: an
unconfigured run must never report success).
"""

import base64
import json
import os
import sys
import uuid
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

load_dotenv()


class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    RESET = '\033[0m'
    BOLD = '\033[1m'


def print_header(text):
    print(f"\n{Colors.BOLD}{Colors.BLUE}{'=' * 70}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{text}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{'=' * 70}{Colors.RESET}\n")


def print_pass(text):
    print(f"{Colors.GREEN}PASS{Colors.RESET}  {text}")


def print_fail(text):
    print(f"{Colors.RED}FAIL{Colors.RESET}  {text}")


def print_warn(text):
    print(f"{Colors.YELLOW}WARN{Colors.RESET}  {text}")


def print_info(text):
    print(f"{Colors.BLUE}INFO{Colors.RESET}  {text}")


def _decode_jwt_claims(token):
    """Decode a JWT's payload without verifying its signature.

    Safe here only because the token was either just minted by THIS script
    moments ago for a dedicated test user, or supplied directly by the
    operator for that same purpose -- this reads our own trusted token to
    extract `sub`, it never verifies an untrusted one.
    """
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return {}


def _mint_session_token(session_id):
    """Mint a fresh session JWT from an EXISTING Clerk session_id via
    Clerk's Backend API. Does not create a session or an account -- the
    session_id must already exist (created once by the operator signing in
    as the dedicated test user)."""
    secret = os.environ.get("CLERK_SECRET_KEY", "").strip()
    if not secret:
        raise RuntimeError(
            "CLERK_SECRET_KEY is required to mint a token from a session_id")
    api_base = os.environ.get(
        "CLERK_API_BASE", "https://api.clerk.com/v1").rstrip("/")
    resp = requests.post(
        f"{api_base}/sessions/{session_id}/tokens",
        headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
        json={},
        timeout=15,
    )
    resp.raise_for_status()
    token = (resp.json() or {}).get("jwt")
    if not token:
        raise RuntimeError(
            f"Clerk did not return a jwt for session {session_id}")
    return token


def _load_test_user(label):
    """Resolve a real Clerk session JWT + user_id for RLS_TEST_USER_<label>.

    Returns None (not an exception) when unconfigured, so the caller can
    report a clear "skipped, here's what to set" message instead of a
    traceback.
    """
    token = os.environ.get(f"RLS_TEST_USER_{label}_TOKEN", "").strip()
    session_id = None
    if not token:
        session_id = os.environ.get(
            f"RLS_TEST_USER_{label}_SESSION_ID", "").strip()
        if not session_id:
            return None
        token = _mint_session_token(session_id)

    claims = _decode_jwt_claims(token)
    user_id = str(claims.get("sub") or "").strip()
    if not user_id:
        return None
    return {
        "token": token,
        "user_id": user_id,
        "issuer": str(claims.get("iss") or ""),
        "session_id": session_id,
    }


def _refresh_test_user_token(user):
    """Mint a fresh token for Layer 2, rather than reusing Layer 1's.

    Clerk session tokens are short-lived by default (~60s). Layer 1 makes
    a dozen-plus sequential Supabase round trips across three tables before
    Layer 2 ever runs, on the SAME token object minted once at the top of
    main() -- confirmed live 2026-08-31 (plan.md §21's Prompt 34 update)
    that this can expire the token well before Layer 2 uses it, surfacing
    as a generic app-layer "Invalid or expired Clerk token" 401 that has
    nothing to do with RLS. Only possible when the user was resolved from a
    session_id (CI path); a statically-supplied RLS_TEST_USER_*_TOKEN has no
    session_id to re-mint from and is reused as-is.
    """
    if not user.get("session_id"):
        return user
    fresh_token = _mint_session_token(user["session_id"])
    return {**user, "token": fresh_token}


def _postgrest_headers(publishable_key, token):
    return {
        "Authorization": f"Bearer {token}",
        "apikey": publishable_key,
        "Content-Type": "application/json",
    }


def _postgrest_select(supabase_url, publishable_key, table, token, filter_user_id, columns):
    url = f"{supabase_url.rstrip('/')}/rest/v1/{table}"
    resp = requests.get(
        url,
        headers=_postgrest_headers(publishable_key, token),
        params={"user_id": f"eq.{filter_user_id}", "select": columns},
        timeout=20,
    )
    return resp


def check_table_rls(supabase_url, publishable_key, table_name, id_column, sentinel_column,
                     user_a, user_b):
    """Layer 1: direct-to-PostgREST positive + negative RLS check for one
    table. Returns (ok: bool, messages: list[str]).
    """
    messages = []
    sentinel = f"rls-verify-{uuid.uuid4().hex[:12]}"
    row_id = str(uuid.uuid4())

    insert_payload = {"user_id": user_a["user_id"], sentinel_column: sentinel}
    if id_column:
        insert_payload[id_column] = row_id

    insert_url = f"{supabase_url.rstrip('/')}/rest/v1/{table_name}"
    insert_resp = requests.post(
        insert_url,
        headers={**_postgrest_headers(publishable_key, user_a["token"]),
                 "Prefer": "return=minimal"},
        json=insert_payload,
        timeout=20,
    )
    if insert_resp.status_code not in (200, 201, 204):
        hint = (
            "either RLS's INSERT policy is rejecting a legitimate owner, or "
            "auth.uid() isn't resolving at all (plan.md §21.1)."
        )
        if insert_resp.status_code == 404 and "PGRST125" in insert_resp.text:
            # Reached PostgREST fine, but it doesn't recognize this path --
            # SUPABASE_URL almost certainly has an unexpected extra segment
            # (e.g. a pasted /rest/v1 suffix doubling into
            # .../rest/v1/rest/v1/<table>; main() already strips that one
            # known case, so seeing this means something else is off).
            hint = (
                f"PostgREST does not recognize this path ({insert_url!r}) -- "
                "not an RLS decision, the request never got that far. "
                "SUPABASE_URL should be the bare project URL "
                "(https://<project-ref>.supabase.co), matching exactly what "
                "app.py's own create_client(SUPABASE_URL, ...) expects -- "
                "the supabase-py client appends /rest/v1 itself, same as "
                "this script does. Double check the SUPABASE_URL secret's "
                "exact value against that."
            )
        elif insert_resp.status_code == 401 and "PGRST301" in insert_resp.text:
            # PostgREST could not verify the JWT's SIGNATURE at all here --
            # RLS was never reached, so this is not the §21.1 "auth.uid()
            # resolves NULL" scenario (that fails the RLS check itself, with
            # a 403/42501, after the JWT verifies fine). PGRST301 means the
            # Third-Party Auth / JWKS trust relationship itself is broken:
            # most commonly, the CLERK_SECRET_KEY used to mint this token
            # belongs to a different Clerk environment (dev vs. prod
            # instance) than the domain Supabase's Third-Party Auth is
            # configured to trust. Compare the printed token issuer above
            # against Supabase Dashboard -> Authentication -> Third-Party
            # Auth's configured Clerk domain -- they must match exactly.
            hint = (
                "PostgREST could not verify this JWT's SIGNATURE at all -- "
                "RLS never got evaluated. This is a Third-Party Auth / JWKS "
                "trust mismatch, not an RLS policy decision. Compare this "
                f"token's issuer ({user_a.get('issuer') or 'unknown, decode failed'}) "
                "against the Clerk domain configured in Supabase Dashboard -> "
                "Authentication -> Third-Party Auth -- they must match "
                "exactly. A common cause: CLERK_SECRET_KEY (used to mint "
                "this token) belongs to a different Clerk environment "
                "(dev vs. prod instance) than the one Supabase trusts."
            )
        return False, [
            f"{table_name}: INSERT as owner (user A) failed with "
            f"{insert_resp.status_code}: {insert_resp.text[:300]} -- {hint}"
        ]

    try:
        # Positive control: owner reading their own row must see it. Also
        # satisfies plan.md §21.2.2 STEP 3's "demonstrate the negative
        # assertion can fail before trusting the positive one" -- this is
        # the same query shape the negative check below uses, run where the
        # answer SHOULD be "yes, data" (against its own author), so a
        # structurally broken query (wrong table/column, auth failure) would
        # show up here as a false negative rather than silently validating
        # an always-empty negative check.
        own_resp = _postgrest_select(
            supabase_url, publishable_key, table_name, user_a["token"],
            user_a["user_id"], sentinel_column)
        if own_resp.status_code not in (200, 206):
            messages.append(
                f"{table_name}: SELECT as owner (user A) failed with "
                f"{own_resp.status_code}: {own_resp.text[:300]}")
            return False, messages
        own_rows = own_resp.json()
        if not any(r.get(sentinel_column) == sentinel for r in own_rows):
            messages.append(
                f"{table_name}: owner's own sentinel row did not come back "
                f"(got {own_rows!r}). This is the silent-zero-rows symptom "
                "plan.md §21.1 warns about."
            )
            return False, messages
        messages.append(f"{table_name}: owner can read their own row (positive control OK)")

        # The negative assertion -- what gives this script teeth.
        cross_resp = _postgrest_select(
            supabase_url, publishable_key, table_name, user_b["token"],
            user_a["user_id"], sentinel_column)
        if cross_resp.status_code not in (200, 206):
            messages.append(
                f"{table_name}: SELECT as user B filtered to user A's rows "
                f"failed with {cross_resp.status_code}: {cross_resp.text[:300]}")
            return False, messages
        cross_rows = cross_resp.json()
        if cross_rows:
            messages.append(
                f"{table_name}: RLS FAILURE -- user B's token retrieved "
                f"{len(cross_rows)} row(s) belonging to user A. RLS is NOT "
                "enforcing cross-user isolation on this table."
            )
            return False, messages
        messages.append(
            f"{table_name}: user B cannot see user A's row (negative assertion OK)")
        return True, messages
    finally:
        # Best-effort cleanup, never affects the pass/fail verdict.
        delete_filter = {id_column: f"eq.{row_id}"} if id_column else {
            "user_id": f"eq.{user_a['user_id']}"}
        try:
            requests.delete(
                f"{supabase_url.rstrip('/')}/rest/v1/{table_name}",
                headers=_postgrest_headers(publishable_key, user_a["token"]),
                params=delete_filter,
                timeout=20,
            )
        except Exception:
            pass


def _vercel_bypass_headers():
    """Bypasses Vercel Deployment Protection (password/SSO-gated preview
    URLs) via Project Settings -> Deployment Protection -> Protection Bypass
    for Automation. This does NOT bypass Vercel's Firewall-level checks
    (Attack Mode, managed bot/DDoS rulesets, custom WAF rules) -- confirmed
    live 2026-08-31 (plan.md §21's Prompt 34 update): Vercel's own docs say
    those "cannot be bypassed even with a valid bypass token." If Layer 2
    still 429s with a Vercel "Security Checkpoint" page after this header is
    set, the cause is a Firewall-level rule, not Deployment Protection --
    check Project -> Firewall -> Firewall Observability for which rule
    matched. Optional: if VERCEL_AUTOMATION_BYPASS_SECRET is unset, Layer 2
    requests are sent without this header."""
    secret = os.environ.get("VERCEL_AUTOMATION_BYPASS_SECRET", "").strip()
    return {"x-vercel-protection-bypass": secret} if secret else {}


_LAYER2_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def check_preferences_app_round_trip(base_url, user_a):
    """Layer 2: the real HTTP round trip through the deployed app."""
    sentinel = f"rls-verify-{uuid.uuid4().hex[:12]}"
    headers = {
        "Authorization": f"Bearer {user_a['token']}",
        "User-Agent": _LAYER2_USER_AGENT,
        **_vercel_bypass_headers(),
    }

    put_resp = requests.put(
        f"{base_url.rstrip('/')}/api/user/preferences",
        headers=headers,
        json={"prefs": {"__rls_verify_sentinel__": sentinel}},
        timeout=20,
    )
    if put_resp.status_code != 200:
        return False, (
            f"PUT /api/user/preferences returned {put_resp.status_code}: "
            f"{put_resp.text[:300]}"
        )

    get_resp = requests.get(
        f"{base_url.rstrip('/')}/api/user/preferences", headers=headers, timeout=20)
    if get_resp.status_code != 200:
        return False, (
            f"GET /api/user/preferences returned {get_resp.status_code}: "
            f"{get_resp.text[:300]}"
        )

    got = ((get_resp.json() or {}).get("prefs") or {}).get("__rls_verify_sentinel__")
    if got != sentinel:
        return False, (
            "Sentinel did not round-trip through /api/user/preferences -- "
            f"expected {sentinel!r}, got {got!r}. This is the exact "
            "silent-zero-rows symptom plan.md §21.1 warns about if "
            "Supabase isn't trusting Clerk's JWT."
        )
    return True, "sentinel round-tripped through /api/user/preferences"


def check_bookmarks_app_round_trip(base_url, user_a):
    sentinel_ref = f"rls-verify-{uuid.uuid4().hex[:12]}"
    headers = {
        "Authorization": f"Bearer {user_a['token']}",
        "User-Agent": _LAYER2_USER_AGENT,
        **_vercel_bypass_headers(),
    }

    post_resp = requests.post(
        f"{base_url.rstrip('/')}/api/bookmarks/semantic",
        headers=headers,
        json={
            "ref": sentinel_ref,
            "label": "RLS verify sentinel",
            "segment_text": "",
            "notes": "",
            # Explicit non-empty ai_summary skips the Gemini summarization
            # call this route would otherwise make -- this script tests
            # RLS, not the AI provider.
            "ai_summary": "rls-verify",
        },
        timeout=20,
    )
    if post_resp.status_code != 200:
        return False, (
            f"POST /api/bookmarks/semantic returned {post_resp.status_code}: "
            f"{post_resp.text[:300]}"
        )

    get_resp = requests.get(
        f"{base_url.rstrip('/')}/api/bookmarks/semantic", headers=headers, timeout=20)
    if get_resp.status_code != 200:
        return False, (
            f"GET /api/bookmarks/semantic returned {get_resp.status_code}: "
            f"{get_resp.text[:300]}"
        )

    items = (get_resp.json() or {}).get("items") or []
    if not any(item.get("ref") == sentinel_ref for item in items):
        return False, (
            "Sentinel bookmark did not round-trip through "
            "/api/bookmarks/semantic -- this is the silent-zero-rows "
            "symptom plan.md §21.1 warns about if Supabase isn't trusting "
            "Clerk's JWT."
        )
    return True, "sentinel round-tripped through /api/bookmarks/semantic"


def main():
    print_header("Sh'elah RLS live acceptance check (plan.md §21.2.2)")

    base_url = (
        os.environ.get("RLS_VERIFY_BASE_URL")
        or os.environ.get("DEPLOYED_URL")
        or os.environ.get("VERCEL_URL")
        or ""
    ).strip()
    supabase_url = (os.environ.get("SUPABASE_URL") or "").strip()
    # app.py's own SUPABASE_URL usage (create_client(SUPABASE_URL, ...))
    # expects the bare project URL -- the supabase-py client appends
    # /rest/v1 itself. If SUPABASE_URL was instead copy-pasted as the full
    # REST endpoint, this script's own f"{supabase_url}/rest/v1/{table}"
    # construction below would double that path, which PostgREST reports as
    # PGRST125 "Invalid path specified in request URL" (confirmed live
    # 2026-08-31, see plan.md §21's Prompt 34 update). Strip it defensively
    # so a pasted REST URL doesn't silently 404 every table identically.
    if supabase_url.rstrip("/").endswith("/rest/v1"):
        supabase_url = supabase_url.rstrip("/")[: -len("/rest/v1")]
    publishable_key = (os.environ.get("SUPABASE_PUBLISHABLE_KEY") or "").strip()

    missing = []
    if not base_url:
        missing.append("RLS_VERIFY_BASE_URL (or DEPLOYED_URL / VERCEL_URL)")
    if not supabase_url:
        missing.append("SUPABASE_URL")
    if not publishable_key:
        missing.append("SUPABASE_PUBLISHABLE_KEY")
    if missing:
        print_fail("Missing required configuration: " + ", ".join(missing))
        return 1

    try:
        user_a = _load_test_user("A")
        user_b = _load_test_user("B")
    except Exception as e:
        print_fail(f"Could not resolve test user tokens: {e}")
        return 1

    if not user_a or not user_b:
        print_fail(
            "RLS_TEST_USER_A_* and RLS_TEST_USER_B_* must both be set "
            "(each as either RLS_TEST_USER_<X>_TOKEN, a pre-minted Clerk "
            "session JWT, or RLS_TEST_USER_<X>_SESSION_ID + CLERK_SECRET_KEY "
            "to mint one fresh). This script never creates a Clerk account "
            "or session -- both test users' sessions must already exist."
        )
        return 1

    if user_a["user_id"] == user_b["user_id"]:
        print_fail(
            "RLS_TEST_USER_A and RLS_TEST_USER_B resolved to the SAME "
            "Clerk user_id -- the negative cross-user assertion is "
            "meaningless without two distinct test users."
        )
        return 1

    print_info(f"Base URL: {base_url}")
    print_info(f"Supabase project: {supabase_url}")
    print_info(f"Test user A: {user_a['user_id']} (token issuer: {user_a['issuer'] or 'unknown -- decode failed'})")
    print_info(f"Test user B: {user_b['user_id']} (token issuer: {user_b['issuer'] or 'unknown -- decode failed'})")

    all_ok = True

    print_header("Layer 1 -- direct Postgres RLS check (positive + negative)")
    tables = [
        (os.environ.get("SUPABASE_PREFS_TABLE") or "user_preferences", None, "prefs"),
        (os.environ.get("SUPABASE_STUDY_BOOKMARKS_TABLE") or "study_bookmarks", "id", "ref"),
        (os.environ.get("SUPABASE_USER_MEMORIES_TABLE") or "user_memories", None, "summary"),
    ]
    for table_name, id_column, sentinel_column in tables:
        ok, messages = check_table_rls(
            supabase_url, publishable_key, table_name, id_column, sentinel_column,
            user_a, user_b)
        last_index = len(messages) - 1
        for i, m in enumerate(messages):
            if i == last_index:
                (print_pass if ok else print_fail)(m)
            else:
                print_info(m)
        all_ok = all_ok and ok

    print_header("Layer 2 -- app-routed smoke check (full plumbing, user A only)")
    try:
        user_a_layer2 = _refresh_test_user_token(user_a)
    except Exception as e:
        print_warn(f"Could not refresh user A's token for Layer 2, reusing Layer 1's: {e}")
        user_a_layer2 = user_a
    for label, check_fn in (
        ("preferences", check_preferences_app_round_trip),
        ("bookmarks", check_bookmarks_app_round_trip),
    ):
        try:
            ok, message = check_fn(base_url, user_a_layer2)
        except Exception as e:
            ok, message = False, f"{label}: unexpected error: {e}"
        (print_pass if ok else print_fail)(message)
        all_ok = all_ok and ok

    print_header("Result")
    if all_ok:
        print_pass("RLS is enforcing cross-user isolation on every checked table.")
    else:
        print_fail(
            "RLS verification FAILED -- see above. Do not treat RLS as a "
            "working defense-in-depth layer until this passes."
        )
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
