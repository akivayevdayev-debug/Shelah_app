"""Tests for scripts/verify_answer_feedback_migration.py.

The script certifies two RLS properties of the answer_feedback table against a
live Supabase project: anon may INSERT, nobody but the service role may
SELECT. These tests feed it fake service/anon clients whose schema snapshot and
row visibility are switchable, and pin that each way the migration can be wrong
turns into a FAIL and a non-zero exit -- not just that the branches execute.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "verify_answer_feedback_migration.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("verify_answer_feedback_migration", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    with patch("dotenv.load_dotenv"):
        spec.loader.exec_module(module)
    return module


vm = _load_script()
TABLE = vm.TABLE

GOOD_TABLE_META = {
    "table_name": "answer_feedback",
    "rls_enabled": True,
    "policies": [{"name": "anyone_can_submit_feedback", "cmd": "INSERT"}],
}


class _Query:
    def __init__(self, client, table):
        self.client, self.table_name = client, table
        self.action = None
        self.filter = None

    def insert(self, row):
        self.action, self.row = "insert", row
        return self

    def select(self, _cols):
        self.action = "select"
        return self

    def delete(self):
        self.action = "delete"
        return self

    def eq(self, column, value):
        self.filter = (column, value)
        return self

    def execute(self):
        return self.client._run(self)


class FakeClient:
    def __init__(self, *, snapshot=None, rpc_error=None, insert_error=None, select_error=None,
                 leaks_rows=False, delete_error=None):
        self.snapshot, self.rpc_error = snapshot, rpc_error
        self.insert_error, self.select_error = insert_error, select_error
        self.leaks_rows, self.delete_error = leaks_rows, delete_error
        self.rows = []
        self.deleted = []

    def rpc(self, name, _params):
        client = self

        class _Rpc:
            def execute(self_inner):
                if client.rpc_error:
                    raise client.rpc_error
                return SimpleNamespace(data=client.snapshot)

        assert name == "get_schema_snapshot"
        return _Rpc()

    def table(self, name):
        assert name == TABLE
        return _Query(self, name)

    def _run(self, q):
        if q.action == "insert":
            if self.insert_error:
                raise self.insert_error
            self.rows.append(q.row)
            return SimpleNamespace(data=[q.row])
        if q.action == "select":
            if self.select_error:
                raise self.select_error
            data = [r for r in self.rows if r[q.filter[0]] == q.filter[1]] if self.leaks_rows else []
            return SimpleNamespace(data=data)
        if self.delete_error:
            raise self.delete_error
        self.deleted.append(q.filter)
        return SimpleNamespace(data=[])


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "secret")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "anon")


def _install(monkeypatch, service, anon):
    clients = {"secret": service, "anon": anon}
    monkeypatch.setattr(vm, "create_client", lambda url, key: clients[key])


def _run(monkeypatch, capsys, service, anon=None):
    _install(monkeypatch, service, anon or FakeClient())
    rc = vm.main()
    return rc, capsys.readouterr().out


def _snapshot(**overrides):
    return {"tables": [{**GOOD_TABLE_META, **overrides}]}


class TestHealthyMigration:
    def test_correct_migration_passes_and_cleans_up_the_probe_row(self, env, monkeypatch, capsys):
        service, anon = FakeClient(snapshot=_snapshot()), FakeClient()

        rc, out = _run(monkeypatch, capsys, service, anon)

        assert rc == 0
        assert "all checks passed" in out
        assert len(anon.rows) == 1
        probe = anon.rows[0]
        assert probe["user_id"] is None
        assert probe["question_hash"].startswith("acceptance-test-")
        assert service.deleted == [("question_hash", probe["question_hash"])]

    def test_select_that_raises_is_an_acceptable_deny(self, env, monkeypatch, capsys):
        anon = FakeClient(select_error=RuntimeError("permission denied for table"))

        rc, out = _run(monkeypatch, capsys, FakeClient(snapshot=_snapshot()), anon)

        assert rc == 0
        assert "also an acceptable deny" in out


class TestBrokenMigrationIsReportedAsFailure:
    def test_rls_disabled(self, env, monkeypatch, capsys):
        rc, out = _run(monkeypatch, capsys, FakeClient(snapshot=_snapshot(rls_enabled=False)))
        assert rc == 1
        assert "RLS is OFF" in out

    @pytest.mark.parametrize("cmd", ["SELECT", "ALL"])
    def test_a_select_capable_policy_fails(self, env, monkeypatch, capsys, cmd):
        policies = [{"name": "anyone_can_submit_feedback", "cmd": "INSERT"},
                    {"name": "readable", "cmd": cmd}]
        rc, out = _run(monkeypatch, capsys, FakeClient(snapshot=_snapshot(policies=policies)))
        assert rc == 1
        assert "SELECT-capable policy" in out
        assert "readable" in out

    def test_several_select_policies_use_the_plural(self, env, monkeypatch, capsys):
        policies = [{"name": "p1", "cmd": "SELECT"}, {"name": "p2", "cmd": "SELECT"},
                    {"name": "ins", "cmd": "INSERT"}]
        rc, out = _run(monkeypatch, capsys, FakeClient(snapshot=_snapshot(policies=policies)))
        assert rc == 1
        assert "SELECT-capable policies" in out

    def test_missing_insert_policy_fails(self, env, monkeypatch, capsys):
        rc, out = _run(monkeypatch, capsys, FakeClient(snapshot=_snapshot(policies=[])))
        assert rc == 1
        assert "has no INSERT policy" in out

    def test_table_absent_from_snapshot_means_migration_not_applied(self, env, monkeypatch, capsys):
        rc, out = _run(monkeypatch, capsys, FakeClient(snapshot={"tables": []}))
        assert rc == 1
        assert "not found by get_schema_snapshot()" in out

    def test_snapshot_rpc_failure_fails_and_points_at_the_introspection_migration(
            self, env, monkeypatch, capsys):
        rc, out = _run(monkeypatch, capsys, FakeClient(rpc_error=RuntimeError("rpc missing")))
        assert rc == 1
        assert "get_schema_snapshot() RPC call failed: rpc missing" in out
        assert "introspect_schema.sql" in out

    def test_anon_insert_failure_fails(self, env, monkeypatch, capsys):
        anon = FakeClient(insert_error=RuntimeError("new row violates row-level security"))
        rc, out = _run(monkeypatch, capsys, FakeClient(snapshot=_snapshot()), anon)
        assert rc == 1
        assert "anon INSERT failed" in out

    def test_anon_reading_back_its_row_is_the_rls_leak_and_fails(self, env, monkeypatch, capsys):
        """The empirical half of the check must have teeth: if anon CAN read the
        row it just wrote, the table is not private, whatever the snapshot says."""
        rc, out = _run(monkeypatch, capsys, FakeClient(snapshot=_snapshot()),
                       FakeClient(leaks_rows=True))
        assert rc == 1
        assert "RLS is NOT blocking SELECT for anon" in out

    def test_failed_probe_cleanup_is_non_fatal(self, env, monkeypatch, capsys):
        service = FakeClient(snapshot=_snapshot(), delete_error=RuntimeError("boom"))
        rc, out = _run(monkeypatch, capsys, service)
        assert rc == 0
        assert "cleanup delete failed (non-fatal" in out

    def test_failures_are_counted_in_the_summary(self, env, monkeypatch, capsys):
        # RLS off + no INSERT policy + leaking anon = 3 independent failures.
        rc, out = _run(monkeypatch, capsys,
                       FakeClient(snapshot=_snapshot(rls_enabled=False, policies=[])),
                       FakeClient(leaks_rows=True))
        assert rc == 1
        assert "3 check(s) failed" in out


class TestConfiguration:
    @pytest.mark.parametrize("missing", ["SUPABASE_URL", "SUPABASE_SECRET_KEY",
                                         "SUPABASE_PUBLISHABLE_KEY"])
    def test_missing_credentials_fail_closed_without_touching_supabase(
            self, env, monkeypatch, capsys, missing):
        monkeypatch.delenv(missing)
        monkeypatch.setattr(vm, "create_client",
                            lambda *a: pytest.fail("must not connect without credentials"))
        assert vm.main() == 1
        assert "must all be set" in capsys.readouterr().out

    def test_output_helpers_tag_each_line_with_its_status(self, capsys):
        vm.ok("a")
        vm.fail("b")
        vm.info("c")
        out = capsys.readouterr().out
        assert "PASS" in out and "FAIL" in out and "INFO" in out


def test_script_entry_point_exits_with_main_return_code(env, monkeypatch):
    import runpy

    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("SUPABASE_SECRET_KEY")
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPT_PATH), run_name="__main__")
    assert exc.value.code == 1
