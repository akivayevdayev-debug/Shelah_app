"""Tests for scripts/verify_rls.py.

The script is the live RLS acceptance check: it asks Postgres (through
PostgREST) whether user B can read user A's rows. Its whole value is that the
negative assertion can FAIL, so these tests drive it against an in-memory
stand-in for PostgREST + the deployed app whose RLS behaviour is switchable
(enforced / leaky / auth.uid()-is-NULL) and pin that the script's verdict and
exit code follow that behaviour, not merely that its code paths execute.
"""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "verify_rls.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("verify_rls", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    with patch("dotenv.load_dotenv"):  # never pull a developer's real .env in
        spec.loader.exec_module(module)
    return module


vr = _load_script()


def _b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _jwt(sub, **claims):
    return f"{_b64({'alg': 'none'})}.{_b64({'sub': sub, **claims})}.sig"


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")


class FakeStack:
    """In-memory PostgREST (with switchable RLS) + deployed app + Clerk API.

    rls: "enforced"  -> a caller only ever sees rows whose user_id is their own
         "leaky"     -> RLS is off: every caller sees every row they filter for
         "blind"     -> auth.uid() resolves NULL: nobody sees anything
    """

    def __init__(self, rls="enforced"):
        self.rls = rls
        self.rows = {}
        self.calls = []
        self.insert_response = None        # (status, text) to force an INSERT failure
        self.select_status = 200
        self.owner_select_status = 200
        self.delete_raises = False
        self.app_put_status = 200
        self.app_get_status = 200
        self.app_persists = True
        self.sessions = {}                 # session_id -> jwt
        self.prefs = {}
        self.bookmarks = []

    # ── helpers ──
    @staticmethod
    def _caller(headers):
        token = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
        return vr._decode_jwt_claims(token).get("sub")

    def _table(self, url):
        return url.rsplit("/", 1)[1]

    # ── requests.* replacements ──
    def post(self, url, headers=None, params=None, json=None, timeout=None):  # noqa: A002
        self.calls.append(("POST", url, headers, params, json))
        if "/sessions/" in url:
            session_id = url.split("/sessions/")[1].split("/")[0]
            if session_id not in self.sessions:
                return _Resp(404, {"errors": ["resource_not_found"]})
            return _Resp(200, {"jwt": self.sessions[session_id]})
        if url.endswith("/api/bookmarks/semantic"):
            if self.app_put_status != 200:
                return _Resp(self.app_put_status, text="app error")
            if self.app_persists:
                self.bookmarks.append({"ref": json["ref"], "owner": self._caller(headers)})
            return _Resp(200, {"ok": True})
        if self.insert_response is not None:
            return _Resp(self.insert_response[0], text=self.insert_response[1])
        table = self._table(url)
        rows = self.rows.setdefault(table, [])
        if params and params.get("on_conflict"):
            rows[:] = [r for r in rows if r["user_id"] != json["user_id"]]
        rows.append(dict(json))
        return _Resp(201)

    def put(self, url, headers=None, json=None, timeout=None):  # noqa: A002
        self.calls.append(("PUT", url, headers, None, json))
        if self.app_put_status != 200:
            return _Resp(self.app_put_status, text="app error")
        if self.app_persists:
            self.prefs[self._caller(headers)] = json["prefs"]
        return _Resp(200, {"ok": True})

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(("GET", url, headers, params, None))
        if url.endswith("/api/user/preferences"):
            if self.app_get_status != 200:
                return _Resp(self.app_get_status, text="app error")
            return _Resp(200, {"prefs": self.prefs.get(self._caller(headers), {})})
        if url.endswith("/api/bookmarks/semantic"):
            if self.app_get_status != 200:
                return _Resp(self.app_get_status, text="app error")
            caller = self._caller(headers)
            return _Resp(200, {"items": [b for b in self.bookmarks if b["owner"] == caller]})
        caller = self._caller(headers)
        is_owner_read = params["user_id"] == f"eq.{caller}"
        status = self.owner_select_status if is_owner_read else self.select_status
        if status != 200:
            return _Resp(status, text="select failed")
        wanted = params["user_id"].removeprefix("eq.")
        column = params["select"]
        rows = [r for r in self.rows.get(self._table(url), []) if r["user_id"] == wanted]
        if self.rls == "blind":
            rows = []
        elif self.rls == "enforced":
            rows = [r for r in rows if r["user_id"] == caller]
        return _Resp(200, [{column: r.get(column)} for r in rows])

    def delete(self, url, headers=None, params=None, timeout=None):
        self.calls.append(("DELETE", url, headers, params, None))
        if self.delete_raises:
            raise requests.ConnectionError("cleanup failed")
        table = self._table(url)
        column, value = next(iter(params.items()))
        value = value.removeprefix("eq.")
        self.rows[table] = [r for r in self.rows.get(table, []) if r.get(column) != value]
        return _Resp(204)


@pytest.fixture
def stack(monkeypatch):
    fake = FakeStack()
    for verb in ("get", "post", "put", "delete"):
        monkeypatch.setattr(vr.requests, verb, getattr(fake, verb))
    for name in ("RLS_VERIFY_BASE_URL", "DEPLOYED_URL", "VERCEL_URL", "SUPABASE_PREFS_TABLE",
                 "SUPABASE_STUDY_BOOKMARKS_TABLE", "SUPABASE_USER_MEMORIES_TABLE",
                 "VERCEL_AUTOMATION_BYPASS_SECRET", "CLERK_SECRET_KEY", "CLERK_API_BASE",
                 "RLS_TEST_USER_A_TOKEN", "RLS_TEST_USER_A_SESSION_ID",
                 "RLS_TEST_USER_B_TOKEN", "RLS_TEST_USER_B_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    return fake


USER_A = {"token": _jwt("user_a", iss="https://clerk.example"), "user_id": "user_a",
          "issuer": "https://clerk.example", "session_id": None}
USER_B = {"token": _jwt("user_b"), "user_id": "user_b", "issuer": "", "session_id": None}
SUPABASE = "https://proj.supabase.co"


# ───────────────────────────── JWT / Clerk plumbing ─────────────────────────────

class TestDecodeJwtClaims:
    def test_reads_the_payload_without_needing_base64_padding(self):
        assert vr._decode_jwt_claims(_jwt("u1", iss="x", n=1))["sub"] == "u1"

    @pytest.mark.parametrize("token", ["", "notajwt", "a.@@@.c", "a..c"])
    def test_garbage_yields_empty_claims_not_an_exception(self, token):
        assert vr._decode_jwt_claims(token) == {}


class TestMintSessionToken:
    def test_requires_the_clerk_secret(self, stack):
        with pytest.raises(RuntimeError, match="CLERK_SECRET_KEY"):
            vr._mint_session_token("sess_1")

    def test_returns_the_jwt_and_calls_the_configured_api_base(self, stack, monkeypatch):
        monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_x")
        monkeypatch.setenv("CLERK_API_BASE", "https://clerk.internal/v1/")
        stack.sessions["sess_1"] = "jwt-1"

        assert vr._mint_session_token("sess_1") == "jwt-1"

        method, url, headers, _params, _body = stack.calls[-1]
        assert (method, url) == ("POST", "https://clerk.internal/v1/sessions/sess_1/tokens")
        assert headers["Authorization"] == "Bearer sk_test_x"

    def test_ended_session_raises_http_error(self, stack, monkeypatch):
        monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_x")
        with pytest.raises(requests.HTTPError):
            vr._mint_session_token("sess_gone")

    def test_response_without_a_jwt_is_an_error(self, stack, monkeypatch):
        monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_x")
        stack.sessions["sess_1"] = ""
        with pytest.raises(RuntimeError, match="did not return a jwt"):
            vr._mint_session_token("sess_1")


class TestLoadTestUser:
    def test_static_token_wins_and_carries_identity(self, stack, monkeypatch):
        monkeypatch.setenv("RLS_TEST_USER_A_TOKEN", _jwt("user_a", iss="https://iss"))
        user = vr._load_test_user("A")
        assert user["user_id"] == "user_a"
        assert user["issuer"] == "https://iss"
        assert user["session_id"] is None

    def test_session_id_is_minted_into_a_token(self, stack, monkeypatch):
        monkeypatch.setenv("RLS_TEST_USER_B_SESSION_ID", "sess_b")
        monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_x")
        stack.sessions["sess_b"] = _jwt("user_b")
        user = vr._load_test_user("B")
        assert user["user_id"] == "user_b"
        assert user["session_id"] == "sess_b"

    def test_unconfigured_user_is_none_not_an_exception(self, stack):
        assert vr._load_test_user("A") is None

    def test_token_without_a_subject_is_none(self, stack, monkeypatch):
        monkeypatch.setenv("RLS_TEST_USER_A_TOKEN", "not.a.jwt")
        assert vr._load_test_user("A") is None


class TestRefreshTestUserToken:
    def test_static_token_users_are_returned_untouched(self, stack):
        assert vr._refresh_test_user_token(USER_A) is USER_A

    def test_session_users_get_a_freshly_minted_token(self, stack, monkeypatch):
        monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_x")
        stack.sessions["sess_a"] = "fresh-token"
        user = {**USER_A, "session_id": "sess_a"}
        refreshed = vr._refresh_test_user_token(user)
        assert refreshed["token"] == "fresh-token"
        assert refreshed["user_id"] == "user_a"
        assert user["token"] == USER_A["token"], "the original must not be mutated"


# ───────────────────────────── Layer 1: direct PostgREST ─────────────────────────────

class TestCheckTableRls:
    def test_enforced_rls_passes_positive_and_negative_controls(self, stack):
        ok, messages = vr.check_table_rls(
            SUPABASE, "pk", "study_bookmarks", "id", "ref", USER_A, USER_B)

        assert ok is True
        assert any("positive control OK" in m for m in messages)
        assert any("negative assertion OK" in m for m in messages)

    def test_leaky_rls_is_reported_as_a_failure_naming_the_leak(self, stack):
        """The negative assertion must have teeth: with RLS off, B reads A's row."""
        stack.rls = "leaky"

        ok, messages = vr.check_table_rls(
            SUPABASE, "pk", "study_bookmarks", "id", "ref", USER_A, USER_B)

        assert ok is False
        assert "RLS FAILURE" in messages[-1]
        assert "user B's token retrieved 1 row(s)" in messages[-1]

    def test_null_auth_uid_is_caught_by_the_positive_control(self, stack):
        """auth.uid() NULL makes RLS return zero rows to everyone -- the
        negative check alone would 'pass'; the owner read must expose it."""
        stack.rls = "blind"

        ok, messages = vr.check_table_rls(
            SUPABASE, "pk", "study_bookmarks", "id", "ref", USER_A, USER_B)

        assert ok is False
        assert "silent-zero-rows" in messages[-1]

    def test_rejected_owner_insert_fails_with_the_rls_hint(self, stack):
        stack.insert_response = (403, "new row violates row-level security policy")

        ok, messages = vr.check_table_rls(
            SUPABASE, "pk", "study_bookmarks", "id", "ref", USER_A, USER_B)

        assert ok is False
        assert len(messages) == 1
        assert "INSERT as owner (user A) failed with 403" in messages[0]
        assert "auth.uid() isn't resolving" in messages[0]

    def test_bad_supabase_url_is_diagnosed_from_pgrst125(self, stack):
        stack.insert_response = (404, '{"code":"PGRST125"}')

        ok, messages = vr.check_table_rls(
            SUPABASE, "pk", "study_bookmarks", "id", "ref", USER_A, USER_B)

        assert ok is False
        assert "does not recognize this path" in messages[0]
        assert "bare project URL" in messages[0]

    def test_unverifiable_jwt_is_diagnosed_from_pgrst301_and_names_the_issuer(self, stack):
        stack.insert_response = (401, '{"code":"PGRST301"}')

        ok, messages = vr.check_table_rls(
            SUPABASE, "pk", "study_bookmarks", "id", "ref", USER_A, USER_B)

        assert ok is False
        assert "could not verify this JWT's SIGNATURE" in messages[0]
        assert "https://clerk.example" in messages[0]

    def test_owner_select_failure_short_circuits(self, stack):
        stack.owner_select_status = 500

        ok, messages = vr.check_table_rls(
            SUPABASE, "pk", "study_bookmarks", "id", "ref", USER_A, USER_B)

        assert ok is False
        assert "SELECT as owner (user A) failed with 500" in messages[-1]

    def test_cross_user_select_failure_is_a_failure_not_a_pass(self, stack):
        stack.select_status = 503

        ok, messages = vr.check_table_rls(
            SUPABASE, "pk", "study_bookmarks", "id", "ref", USER_A, USER_B)

        assert ok is False
        assert "SELECT as user B filtered to user A's rows failed with 503" in messages[-1]

    def test_sentinel_row_is_cleaned_up_even_when_the_check_fails(self, stack):
        stack.rls = "leaky"

        vr.check_table_rls(SUPABASE, "pk", "study_bookmarks", "id", "ref", USER_A, USER_B)

        assert stack.rows["study_bookmarks"] == []

    def test_cleanup_errors_never_change_the_verdict(self, stack):
        stack.delete_raises = True

        ok, _ = vr.check_table_rls(
            SUPABASE, "pk", "study_bookmarks", "id", "ref", USER_A, USER_B)

        assert ok is True

    def test_single_row_per_user_tables_upsert_so_a_stale_sentinel_cannot_409(self, stack):
        stack.rows["user_preferences"] = [{"user_id": "user_a", "prefs": "stale-from-earlier-run"}]

        ok, _ = vr.check_table_rls(
            SUPABASE, "pk", "user_preferences", None, "prefs", USER_A, USER_B)

        assert ok is True
        insert = next(c for c in stack.calls if c[0] == "POST")
        assert insert[2]["Prefer"] == "return=minimal,resolution=merge-duplicates"
        assert insert[3] == {"on_conflict": "user_id"}
        assert "id" not in insert[4]

    def test_trailing_slash_on_the_supabase_url_does_not_double_up(self, stack):
        vr.check_table_rls(SUPABASE + "/", "pk", "study_bookmarks", "id", "ref", USER_A, USER_B)

        assert all("//rest" not in c[1] for c in stack.calls)


# ───────────────────────────── Layer 2: app round trips ─────────────────────────────

class TestAppRoundTrips:
    @pytest.mark.parametrize("check, label", [
        (vr.check_preferences_app_round_trip, "/api/user/preferences"),
        (vr.check_bookmarks_app_round_trip, "/api/bookmarks/semantic"),
    ])
    def test_healthy_round_trip_passes(self, stack, check, label):
        ok, message = check("https://app.example/", USER_A)
        assert ok is True
        assert label in message

    @pytest.mark.parametrize("check", [vr.check_preferences_app_round_trip,
                                       vr.check_bookmarks_app_round_trip])
    def test_write_rejected_is_a_failure(self, stack, check):
        stack.app_put_status = 401
        ok, message = check("https://app.example", USER_A)
        assert ok is False
        assert "returned 401" in message

    @pytest.mark.parametrize("check", [vr.check_preferences_app_round_trip,
                                       vr.check_bookmarks_app_round_trip])
    def test_read_rejected_is_a_failure(self, stack, check):
        stack.app_get_status = 500
        ok, message = check("https://app.example", USER_A)
        assert ok is False
        assert "GET" in message
        assert "returned 500" in message

    @pytest.mark.parametrize("check", [vr.check_preferences_app_round_trip,
                                       vr.check_bookmarks_app_round_trip])
    def test_silently_dropped_write_is_the_silent_zero_rows_failure(self, stack, check):
        stack.app_persists = False
        ok, message = check("https://app.example", USER_A)
        assert ok is False
        assert "did not round-trip" in message

    def test_vercel_bypass_header_is_sent_only_when_configured(self, stack, monkeypatch):
        vr.check_preferences_app_round_trip("https://app.example", USER_A)
        assert all("x-vercel-protection-bypass" not in c[2] for c in stack.calls)

        monkeypatch.setenv("VERCEL_AUTOMATION_BYPASS_SECRET", " bypass-secret ")
        stack.calls.clear()
        vr.check_preferences_app_round_trip("https://app.example", USER_A)
        assert all(c[2]["x-vercel-protection-bypass"] == "bypass-secret" for c in stack.calls)


# ───────────────────────────── main(): configuration + exit codes ─────────────────────────────

@pytest.fixture
def configured(stack, monkeypatch):
    monkeypatch.setenv("RLS_VERIFY_BASE_URL", "https://app.example")
    monkeypatch.setenv("SUPABASE_URL", SUPABASE)
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "pk")
    monkeypatch.setenv("RLS_TEST_USER_A_TOKEN", USER_A["token"])
    monkeypatch.setenv("RLS_TEST_USER_B_TOKEN", USER_B["token"])
    return stack


class TestMain:
    def test_all_green_exits_zero(self, configured, capsys):
        assert vr.main() == 0
        out = capsys.readouterr().out
        assert "RLS is enforcing cross-user isolation on every checked table." in out

    def test_leaky_rls_exits_one_and_says_do_not_trust_rls(self, configured, capsys):
        configured.rls = "leaky"
        assert vr.main() == 1
        out = capsys.readouterr().out
        assert "RLS FAILURE" in out
        assert "RLS verification FAILED" in out

    def test_layer_two_failure_alone_still_fails_the_run(self, configured):
        configured.app_persists = False
        assert vr.main() == 1

    def test_layer_two_exception_is_reported_and_fails(self, configured, monkeypatch, capsys):
        def boom(base_url, user):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(vr, "check_bookmarks_app_round_trip", boom)
        assert vr.main() == 1
        assert "bookmarks: unexpected error: connection reset" in capsys.readouterr().out

    @pytest.mark.parametrize("missing", ["RLS_VERIFY_BASE_URL", "SUPABASE_URL",
                                         "SUPABASE_PUBLISHABLE_KEY"])
    def test_missing_required_config_fails_closed_and_names_it(self, configured, monkeypatch,
                                                               capsys, missing):
        monkeypatch.delenv(missing)
        assert vr.main() == 1
        assert missing in capsys.readouterr().out

    def test_base_url_falls_back_to_deployed_url(self, configured, monkeypatch, capsys):
        monkeypatch.delenv("RLS_VERIFY_BASE_URL")
        monkeypatch.setenv("DEPLOYED_URL", "https://fallback.example")
        assert vr.main() == 0
        assert "Base URL: https://fallback.example" in capsys.readouterr().out

    def test_pasted_rest_v1_suffix_is_stripped_so_paths_do_not_double(self, configured,
                                                                      monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", SUPABASE + "/rest/v1/")
        assert vr.main() == 0
        assert not any("/rest/v1/rest/v1" in c[1] for c in configured.calls)

    def test_missing_second_user_fails_closed(self, configured, monkeypatch, capsys):
        monkeypatch.delenv("RLS_TEST_USER_B_TOKEN")
        assert vr.main() == 1
        assert "must both be set" in capsys.readouterr().out

    def test_same_user_twice_is_refused_because_the_negative_check_would_be_meaningless(
            self, configured, monkeypatch, capsys):
        monkeypatch.setenv("RLS_TEST_USER_B_TOKEN", USER_A["token"])
        assert vr.main() == 1
        assert "SAME" in capsys.readouterr().out

    def test_token_minting_failure_is_a_clean_failure_not_a_traceback(self, configured,
                                                                     monkeypatch, capsys):
        monkeypatch.delenv("RLS_TEST_USER_A_TOKEN")
        monkeypatch.setenv("RLS_TEST_USER_A_SESSION_ID", "sess_gone")
        monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_x")
        assert vr.main() == 1
        assert "Could not resolve test user tokens" in capsys.readouterr().out

    def test_layer_two_token_refresh_failure_warns_and_reuses_layer_one_token(
            self, configured, monkeypatch, capsys):
        monkeypatch.setattr(vr, "_refresh_test_user_token",
                            lambda user: (_ for _ in ()).throw(RuntimeError("clerk down")))
        assert vr.main() == 0
        assert "Could not refresh user A's token for Layer 2" in capsys.readouterr().out

    def test_table_names_can_be_overridden_by_environment(self, configured, monkeypatch):
        monkeypatch.setenv("SUPABASE_STUDY_BOOKMARKS_TABLE", "study_bookmarks_v2")
        assert vr.main() == 0
        assert any(c[1].endswith("/rest/v1/study_bookmarks_v2") for c in configured.calls)

    def test_prints_use_the_status_prefixes_operators_grep_for(self, capsys):
        for fn in (vr.print_pass, vr.print_fail, vr.print_warn, vr.print_info):
            fn("msg")
        out = capsys.readouterr().out
        for prefix in ("PASS", "FAIL", "WARN", "INFO"):
            assert prefix in out


def test_script_entry_point_exits_with_main_return_code(configured, monkeypatch):
    """`if __name__ == '__main__': sys.exit(main())` -- run the file as a script."""
    import runpy

    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    configured.rls = "leaky"
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPT_PATH), run_name="__main__")
    assert exc.value.code == 1
