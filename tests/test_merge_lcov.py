"""Behavior tests for scripts/merge_lcov.py (duplicate lcov records -> one)."""

import importlib.util
import io
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "merge_lcov.py"
_SPEC = importlib.util.spec_from_file_location("merge_lcov", SCRIPT)
merge_lcov = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(merge_lcov)


def _record(source, da, brda=()):
    lines = [f"SF:{source}"]
    lines += [f"DA:{n},{h}" for n, h in da]
    lines += [f"BRDA:{n},{b},{k},{t}" for n, b, k, t in brda]
    lines.append("end_of_record")
    return "\n".join(lines) + "\n"


def test_duplicate_records_sum_their_hit_counts():
    # Two tests each loaded their own copy of the module: one hit line 1, the
    # other line 2. The merged file must count BOTH lines as covered.
    text = _record("static/js/a.js", [(1, 1), (2, 0), (3, 0)]) + \
        _record("static/js/a.js", [(1, 0), (2, 4), (3, 0)])

    merged = merge_lcov.merge_lcov(text)

    assert merged.count("SF:static/js/a.js") == 1
    assert "DA:1,1" in merged
    assert "DA:2,4" in merged
    assert "DA:3,0" in merged
    assert "LF:3" in merged
    assert "LH:2" in merged


def test_distinct_files_stay_separate_in_first_seen_order():
    text = _record("b.js", [(1, 1)]) + _record("a.js", [(1, 0)]) + _record("b.js", [(2, 1)])

    merged = merge_lcov.merge_lcov(text)

    assert merged.index("SF:b.js") < merged.index("SF:a.js")
    assert merged.count("SF:b.js") == 1
    assert "LH:2" in merged.split("SF:a.js")[0]  # b.js: both lines covered
    assert "LH:0" in merged.split("SF:a.js")[1]  # a.js: nothing covered


def test_branch_hits_are_summed_and_untaken_dash_counts_as_zero():
    text = _record("a.js", [(1, 1)], brda=[(1, "0", "0", "-"), (1, "0", "1", 2)]) + \
        _record("a.js", [(1, 1)], brda=[(1, "0", "0", 3), (1, "0", "1", "-")])

    merged = merge_lcov.merge_lcov(text)

    assert "BRDA:1,0,0,3" in merged
    assert "BRDA:1,0,1,2" in merged
    assert "BRF:2" in merged
    assert "BRH:2" in merged


def test_branch_records_without_a_line_number_are_dropped():
    # Node's lcov reporter emits `BRDA:undefined,...` for branches it cannot
    # place; feeding that to the scanner is worse than omitting it.
    text = _record("a.js", [(1, 1)], brda=[("undefined", "27", "0", 0), (1, "0", "0", 1)])

    merged = merge_lcov.merge_lcov(text)

    assert "undefined" not in merged
    assert "BRDA:1,0,0,1" in merged
    assert "BRF:1" in merged


def test_no_branch_data_emits_no_branch_summary():
    assert "BRF" not in merge_lcov.merge_lcov(_record("a.js", [(1, 1)]))


def test_empty_input_yields_empty_output():
    assert merge_lcov.merge_lcov("") == ""


def test_lines_outside_any_record_are_ignored():
    merged = merge_lcov.merge_lcov("TN:\nDA:1,1\n" + _record("a.js", [(2, 1)]))

    assert "DA:1" not in merged
    assert "DA:2,1" in merged


def _run_main(monkeypatch, capsysbinary, stdin_text, argv=("merge_lcov.py",)):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_text.encode("utf-8"))))
    rc = merge_lcov.main(list(argv))
    return rc, capsysbinary.readouterr()


def test_main_merges_stdin_to_stdout(monkeypatch, capsysbinary):
    raw = _record("a.js", [(1, 1)]) + _record("a.js", [(1, 2)])

    rc, captured = _run_main(monkeypatch, capsysbinary, raw)

    assert rc == 0
    assert captured.out.decode("utf-8").count("DA:1,3") == 1
    assert captured.err == b""


def test_main_keeps_non_ascii_source_paths_intact(monkeypatch, capsysbinary):
    raw = _record("static/js/\u05e9\u05dc.js", [(1, 1)]) + _record("static/js/\u05e9\u05dc.js", [(1, 1)])

    rc, captured = _run_main(monkeypatch, capsysbinary, raw)

    assert rc == 0
    assert "SF:static/js/\u05e9\u05dc.js\n" in captured.out.decode("utf-8")


def test_the_npm_pipeline_shape_works_end_to_end(tmp_path):
    """`python3 scripts/merge_lcov.py < raw > merged` is what package.json runs."""
    raw = tmp_path / "raw.info"
    raw.write_text(_record("a.js", [(1, 1)]) + _record("a.js", [(1, 2)]), encoding="utf-8")
    merged = tmp_path / "lcov.info"

    with raw.open("rb") as stdin, merged.open("wb") as stdout:
        done = subprocess.run([sys.executable, str(SCRIPT)], stdin=stdin, stdout=stdout,
                              stderr=subprocess.PIPE, check=False)

    assert done.returncode == 0, done.stderr
    assert merged.read_text(encoding="utf-8").count("DA:1,3") == 1


@pytest.mark.parametrize("argv", [["merge_lcov.py", "in.info"], ["merge_lcov.py", "in.info", "out.info"]])
def test_main_rejects_the_old_path_arguments(argv, monkeypatch, capsysbinary):
    rc, captured = _run_main(monkeypatch, capsysbinary, _record("a.js", [(1, 1)]), argv)

    assert rc == 2
    assert captured.out == b""
    assert b"Usage: merge_lcov.py < INPUT.info > OUTPUT.info" in captured.err
