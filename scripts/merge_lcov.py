#!/usr/bin/env python3
"""Merge duplicate per-file records in an lcov tracefile into one record each.

Why this exists: Node's lcov reporter writes one `SF:` record per script V8
saw, and several scripts can share one source file (the same file imported at
different URLs). SonarCloud does not merge those (it keeps one record per
file), so the reported coverage would be whatever a single record happened to
hit. This script folds all records of the same file into one.

tests_js/helpers/esm_harness.js used to be such a source (one
`?esmHarnessCtx=<n>` copy per test); it now rebases its V8 coverage so Node
merges the copies itself, and this script is the safety net for any record
that still arrives duplicated.

Lines (DA) are summed. Branches (BRDA) cannot be matched by their ids: Node
writes `BRDA:<line>,<j>,0,<taken>` where <j> is just the entry's index within
its record, and a record only lists the V8 block ranges its copy reported, so
one source branch gets different ids in different records. What does match
across records is the line (SonarCloud also scores branches per line). So each
line's entries are merged by rank: every record's taken-counts for that line
are sorted descending and summed position by position. The line keeps as many
branches as the record that listed the most, and as many covered branches as
the record that covered the most, never more. That is conservative: a line
counts a branch as covered only if one copy alone covered that many, so two
copies that each took a different branch of the same line under-report rather
than over-report. Block ids are renumbered in output order.

It is a stdin -> stdout filter on purpose: the shell that runs it (npm's
`test:coverage` script) names the files, so the script never builds a
filesystem path from its own arguments.

Usage: merge_lcov.py < INPUT.info > OUTPUT.info
"""

import sys
from collections import defaultdict


def _add_line_hits(per_line: dict[int, int], payload: str) -> None:
    """Add one `DA:<line>,<hits>` payload into the per-line hit counts."""
    line_no, hits = payload.split(",")[:2]
    per_line[int(line_no)] = per_line.get(int(line_no), 0) + int(hits)


def _add_branch_hits(per_line: dict[int, list[int]], payload: str) -> None:
    """Add one `BRDA:<line>,<block>,<branch>,<taken>` payload to its line's taken-counts."""
    line_no, _block, _branch, taken = payload.split(",")[:4]
    if not line_no.isdigit():
        # Node's reporter can emit `BRDA:undefined,...` for a branch it
        # cannot map back to a source line; there is nothing to report.
        return
    per_line.setdefault(int(line_no), []).append(0 if taken == "-" else int(taken))


def _merge_branch_records(records: list[dict[int, list[int]]]) -> dict[int, list[int]]:
    """Merge each line's per-record taken-counts by rank (see the module docstring)."""
    merged: dict[int, list[int]] = {}
    for record in records:
        for line_no, taken in record.items():
            ranked = sorted(taken, reverse=True)
            current = merged.setdefault(line_no, [])
            current.extend([0] * (len(ranked) - len(current)))
            for rank, hits in enumerate(ranked):
                current[rank] += hits
    return merged


def _render_record(
    source: str,
    line_hits: dict[int, int],
    branch_hits: dict[int, list[int]],
) -> list[str]:
    """The lcov lines for one merged source-file record."""
    out = [f"SF:{source}"]
    for line_no in sorted(line_hits):
        out.append(f"DA:{line_no},{line_hits[line_no]}")
    out.append(f"LF:{len(line_hits)}")
    out.append(f"LH:{sum(1 for hits in line_hits.values() if hits > 0)}")
    all_taken = [hits for line_no in sorted(branch_hits) for hits in branch_hits[line_no]]
    block = 0
    for line_no in sorted(branch_hits):
        for hits in branch_hits[line_no]:
            out.append(f"BRDA:{line_no},{block},0,{hits}")
            block += 1
    if all_taken:
        out.append(f"BRF:{len(all_taken)}")
        out.append(f"BRH:{sum(1 for hits in all_taken if hits > 0)}")
    out.append("end_of_record")
    return out


def merge_lcov(text: str) -> str:
    """Return `text` (lcov tracefile contents) with one record per source file."""
    lines_by_file: dict[str, dict[int, int]] = defaultdict(dict)
    branch_records_by_file: dict[str, list[dict[int, list[int]]]] = defaultdict(list)
    order: list[str] = []
    current: str | None = None

    for raw in text.splitlines():
        record = raw.strip()
        if record.startswith("SF:"):
            current = record[3:]
            if current not in lines_by_file:
                order.append(current)
                lines_by_file[current] = {}
            branch_records_by_file[current].append({})
        elif current is None:
            continue
        elif record.startswith("DA:"):
            _add_line_hits(lines_by_file[current], record[3:])
        elif record.startswith("BRDA:"):
            _add_branch_hits(branch_records_by_file[current][-1], record[5:])
        elif record == "end_of_record":
            current = None

    out: list[str] = []
    for source in order:
        branches = _merge_branch_records(branch_records_by_file[source])
        out.extend(_render_record(source, lines_by_file[source], branches))
    return "\n".join(out) + ("\n" if out else "")


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        # Refuse the old `merge_lcov.py IN OUT` form instead of ignoring the
        # arguments: a caller that still passes paths would otherwise get an
        # empty result written to stdout and no file at all.
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    merged = merge_lcov(sys.stdin.buffer.read().decode("utf-8"))
    sys.stdout.buffer.write(merged.encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
