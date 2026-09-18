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

Usage: merge_lcov.py INPUT.info OUTPUT.info
"""

import sys
from collections import defaultdict
from pathlib import Path


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
            line_no, hits = record[3:].split(",")[:2]
            per_line = lines_by_file[current]
            per_line[int(line_no)] = per_line.get(int(line_no), 0) + int(hits)
        elif record.startswith("BRDA:"):
            line_no, block, branch, taken = record[5:].split(",")[:4]
            if not line_no.isdigit():
                # Node's reporter can emit `BRDA:undefined,...` for a branch it
                # cannot map back to a source line; there is nothing to report.
                continue
            key = (int(line_no), block, branch)
            per_branch = branches_by_file[current]
            per_branch[key] = per_branch.get(key, 0) + (0 if taken == "-" else int(taken))
        elif record == "end_of_record":
            current = None

    out: list[str] = []
    for source in order:
        out.append(f"SF:{source}")
        line_hits = lines_by_file[source]
        for line_no in sorted(line_hits):
            out.append(f"DA:{line_no},{line_hits[line_no]}")
        out.append(f"LF:{len(line_hits)}")
        out.append(f"LH:{sum(1 for hits in line_hits.values() if hits > 0)}")
        branch_hits = branches_by_file[source]
        for line_no, block, branch in sorted(branch_hits):
            out.append(f"BRDA:{line_no},{block},{branch},{branch_hits[(line_no, block, branch)]}")
        if branch_hits:
            out.append(f"BRF:{len(branch_hits)}")
            out.append(f"BRH:{sum(1 for hits in branch_hits.values() if hits > 0)}")
        out.append("end_of_record")
    return "\n".join(out) + ("\n" if out else "")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    source, destination = Path(argv[1]), Path(argv[2])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(merge_lcov(source.read_text(encoding="utf-8")), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
