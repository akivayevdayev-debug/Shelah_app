#!/usr/bin/env python3
"""Merge duplicate per-file records in an lcov tracefile into one record each.

Why this exists: tests_js/helpers/esm_harness.js gives every test its own copy
of the module under test (a fresh `?esmHarnessCtx=<n>` import) so state never
leaks between tests. Node's coverage reporter treats each copy as its own
source, so `node --test --experimental-test-coverage --test-reporter=lcov`
emits several `SF:static/js/zmanim.js` records, each with only the lines that
one test exercised. SonarCloud does not merge those (it keeps one record per
file), so the reported coverage would be whatever a single test happened to hit.
This script sums the hit counts per line (DA) and per branch (BRDA) across all
records of the same file and writes one record per file.

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


def _add_branch_hits(per_branch: dict[tuple[int, str, str], int], payload: str) -> None:
    """Add one `BRDA:<line>,<block>,<branch>,<taken>` payload into the branch hit counts."""
    line_no, block, branch, taken = payload.split(",")[:4]
    if not line_no.isdigit():
        # Node's reporter can emit `BRDA:undefined,...` for a branch it
        # cannot map back to a source line; there is nothing to report.
        return
    key = (int(line_no), block, branch)
    per_branch[key] = per_branch.get(key, 0) + (0 if taken == "-" else int(taken))


def _render_record(
    source: str,
    line_hits: dict[int, int],
    branch_hits: dict[tuple[int, str, str], int],
) -> list[str]:
    """The lcov lines for one merged source-file record."""
    out = [f"SF:{source}"]
    for line_no in sorted(line_hits):
        out.append(f"DA:{line_no},{line_hits[line_no]}")
    out.append(f"LF:{len(line_hits)}")
    out.append(f"LH:{sum(1 for hits in line_hits.values() if hits > 0)}")
    for line_no, block, branch in sorted(branch_hits):
        out.append(f"BRDA:{line_no},{block},{branch},{branch_hits[(line_no, block, branch)]}")
    if branch_hits:
        out.append(f"BRF:{len(branch_hits)}")
        out.append(f"BRH:{sum(1 for hits in branch_hits.values() if hits > 0)}")
    out.append("end_of_record")
    return out


def merge_lcov(text: str) -> str:
    """Return `text` (lcov tracefile contents) with one record per source file."""
    lines_by_file: dict[str, dict[int, int]] = defaultdict(dict)
    branches_by_file: dict[str, dict[tuple[int, str, str], int]] = defaultdict(dict)
    order: list[str] = []
    current: str | None = None

    for raw in text.splitlines():
        record = raw.strip()
        if record.startswith("SF:"):
            current = record[3:]
            if current not in lines_by_file:
                order.append(current)
                lines_by_file[current] = {}
                branches_by_file[current] = {}
        elif current is None:
            continue
        elif record.startswith("DA:"):
            _add_line_hits(lines_by_file[current], record[3:])
        elif record.startswith("BRDA:"):
            _add_branch_hits(branches_by_file[current], record[5:])
        elif record == "end_of_record":
            current = None

    out: list[str] = []
    for source in order:
        out.extend(_render_record(source, lines_by_file[source], branches_by_file[source]))
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
