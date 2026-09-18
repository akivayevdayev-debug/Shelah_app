"""Tests for scripts/recompute_ai_usage_log_cost.py.

This one-off backfill rewrites cost_usd on financial-ledger rows, so the
properties worth pinning are: the price it recomputes is right (checked against
hand-computed dollar amounts, not against estimate_cost_usd itself), only rows
whose cost actually changes are touched, a dry run never writes, and --execute
writes exactly the computed values.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "recompute_ai_usage_log_cost.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("recompute_ai_usage_log_cost", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rc = _load_script()

# claude-sonnet-4-6 is $3.00/M input and $15.00/M output, so
# 1,000,000 in + 100,000 out = 3.00 + 1.50 = $4.50 exactly.
SONNET_ROW = {"id": 1, "model": "claude-sonnet-4-6", "input_tokens": 1_000_000,
              "output_tokens": 100_000, "cost_usd": 0.0}


class _FakeTable:
    def __init__(self, client):
        self.client = client
        self.mode = None

    def select(self, columns):
        self.mode, self.columns = "select", columns
        return self

    def range(self, start, end):
        self.start, self.end = start, end
        return self

    def update(self, values):
        self.mode, self.values = "update", values
        return self

    def eq(self, column, value):
        self.match = (column, value)
        return self

    def execute(self):
        if self.mode == "select":
            self.client.selects.append((self.start, self.end, self.columns))
            return SimpleNamespace(data=self.client.rows[self.start:self.end + 1])
        self.client.updates.append((self.match, self.values))
        return SimpleNamespace(data=[])


class FakeClient:
    def __init__(self, rows):
        self.rows = rows
        self.selects = []
        self.updates = []

    def table(self, name):
        assert name == "ai_usage_log"
        return _FakeTable(self)


class TestResolveSupabaseConfig:
    def test_returns_trimmed_url_and_key(self, monkeypatch):
        monkeypatch.setattr(rc, "load_dotenv", lambda: None)
        monkeypatch.setenv("SUPABASE_URL", "  https://p.supabase.co ")
        monkeypatch.setenv("SUPABASE_SECRET_KEY", " sk ")
        assert rc.resolve_supabase_config() == {"url": "https://p.supabase.co", "secret_key": "sk"}

    def test_missing_url_is_an_error(self, monkeypatch):
        monkeypatch.setattr(rc, "load_dotenv", lambda: None)
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.setenv("SUPABASE_SECRET_KEY", "sk")
        with pytest.raises(RuntimeError, match="Missing SUPABASE_URL"):
            rc.resolve_supabase_config()

    def test_missing_service_role_key_is_an_error_that_explains_why(self, monkeypatch):
        monkeypatch.setattr(rc, "load_dotenv", lambda: None)
        monkeypatch.setenv("SUPABASE_URL", "https://p.supabase.co")
        monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
        with pytest.raises(RuntimeError, match="service-role key is required"):
            rc.resolve_supabase_config()


class TestFetchAllRows:
    def test_pages_until_a_short_page(self, monkeypatch):
        monkeypatch.setattr(rc, "_PAGE_SIZE", 2)
        client = FakeClient([{"id": i} for i in range(5)])

        rows = rc.fetch_all_rows(client)

        assert [r["id"] for r in rows] == [0, 1, 2, 3, 4]
        assert [(s, e) for s, e, _ in client.selects] == [(0, 1), (2, 3), (4, 5)]

    def test_exact_multiple_of_page_size_needs_one_extra_empty_fetch(self, monkeypatch):
        monkeypatch.setattr(rc, "_PAGE_SIZE", 2)
        client = FakeClient([{"id": i} for i in range(4)])

        assert len(rc.fetch_all_rows(client)) == 4
        assert len(client.selects) == 3

    def test_non_list_payload_is_treated_as_empty(self):
        class _NoneData(FakeClient):
            def table(self, name):
                table = super().table(name)
                table.execute = lambda: SimpleNamespace(data=None)
                return table

        assert rc.fetch_all_rows(_NoneData([])) == []

    def test_page_cap_stops_the_loop_and_warns_on_stderr(self, monkeypatch, capsys):
        monkeypatch.setattr(rc, "_PAGE_SIZE", 1)
        monkeypatch.setattr(rc, "_MAX_PAGES", 3)
        client = FakeClient([{"id": i} for i in range(10)])

        rows = rc.fetch_all_rows(client)

        assert len(rows) == 3
        assert "3-page safety cap" in capsys.readouterr().err


class TestBuildUpdates:
    def test_zero_cost_row_is_repriced_to_the_hand_computed_amount(self):
        updates, old_total, new_total = rc.build_updates([SONNET_ROW])

        assert updates == [{"id": 1, "old_cost_usd": 0.0, "new_cost_usd": 4.5}]
        assert old_total == 0.0
        assert new_total == pytest.approx(4.5)

    def test_rows_already_correct_are_left_alone(self):
        correct = {**SONNET_ROW, "cost_usd": 4.5}
        updates, old_total, new_total = rc.build_updates([correct])

        assert updates == []
        assert old_total == pytest.approx(4.5)
        assert new_total == pytest.approx(4.5)

    def test_free_tier_model_stays_at_zero_so_it_is_not_rewritten(self):
        row = {"id": 2, "model": "gemini-1.5-flash", "input_tokens": 5000,
               "output_tokens": 500, "cost_usd": 0.0}
        assert rc.build_updates([row])[0] == []

    def test_missing_or_null_fields_do_not_crash_and_price_as_zero(self):
        row = {"id": 3, "model": None, "input_tokens": None, "output_tokens": None,
               "cost_usd": None}
        assert rc.build_updates([row]) == ([], 0.0, 0.0)

    def test_a_wrongly_priced_nonzero_row_is_also_corrected(self):
        overpaid = {**SONNET_ROW, "id": 4, "cost_usd": 9.99}
        updates, old_total, new_total = rc.build_updates([overpaid])
        assert updates[0]["old_cost_usd"] == 9.99
        assert updates[0]["new_cost_usd"] == 4.5
        assert new_total - old_total == pytest.approx(4.5 - 9.99)

    def test_totals_span_all_rows_not_only_updated_ones(self):
        rows = [SONNET_ROW, {**SONNET_ROW, "id": 5, "cost_usd": 4.5}]
        updates, old_total, new_total = rc.build_updates(rows)
        assert len(updates) == 1
        assert old_total == pytest.approx(4.5)
        assert new_total == pytest.approx(9.0)


class TestApplyUpdates:
    def test_each_update_targets_only_its_own_row_id(self):
        client = FakeClient([])
        rc.apply_updates(client, [{"id": 7, "new_cost_usd": 1.25}, {"id": 8, "new_cost_usd": 2.5}])
        assert client.updates == [(("id", 7), {"cost_usd": 1.25}), (("id", 8), {"cost_usd": 2.5})]


class TestMain:
    @pytest.fixture
    def wired(self, monkeypatch):
        client = FakeClient([SONNET_ROW])
        monkeypatch.setattr(rc, "resolve_supabase_config",
                            lambda: {"url": "https://p", "secret_key": "sk"})
        monkeypatch.setitem(sys.modules, "supabase",
                            SimpleNamespace(create_client=lambda url, key: client))
        return client

    def test_default_is_a_dry_run_that_writes_nothing(self, wired, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["recompute"])

        rc.main()

        out = capsys.readouterr().out
        assert wired.updates == []
        assert "DRY RUN" in out
        assert "Rows needing a cost_usd correction: 1 / 1" in out
        assert "id=1 0.000000 -> 4.500000" in out

    def test_execute_writes_exactly_the_recomputed_values(self, wired, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["recompute", "--execute"])

        rc.main()

        assert wired.updates == [(("id", 1), {"cost_usd": 4.5})]
        assert "Applying 1 updates" in capsys.readouterr().out

    def test_nothing_to_update_returns_before_any_write(self, monkeypatch, capsys):
        client = FakeClient([{**SONNET_ROW, "cost_usd": 4.5}])
        monkeypatch.setattr(rc, "resolve_supabase_config",
                            lambda: {"url": "https://p", "secret_key": "sk"})
        monkeypatch.setitem(sys.modules, "supabase",
                            SimpleNamespace(create_client=lambda url, key: client))
        monkeypatch.setattr(sys, "argv", ["recompute", "--execute"])

        rc.main()

        assert client.updates == []
        assert "Nothing to update." in capsys.readouterr().out

    def test_dry_run_lists_at_most_ten_samples(self, monkeypatch, capsys):
        rows = [{**SONNET_ROW, "id": i} for i in range(13)]
        client = FakeClient(rows)
        monkeypatch.setattr(rc, "resolve_supabase_config",
                            lambda: {"url": "https://p", "secret_key": "sk"})
        monkeypatch.setitem(sys.modules, "supabase",
                            SimpleNamespace(create_client=lambda url, key: client))
        monkeypatch.setattr(sys, "argv", ["recompute"])

        rc.main()

        out = capsys.readouterr().out
        assert out.count("  id=") == 10
        assert "... and 3 more" in out
        assert client.updates == []


def test_script_entry_point_runs_main(monkeypatch):
    import runpy

    monkeypatch.setattr(sys, "argv", ["recompute"])
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    with patch("dotenv.load_dotenv"), pytest.raises(RuntimeError, match="Missing SUPABASE_URL"):
        runpy.run_path(str(SCRIPT_PATH), run_name="__main__")
