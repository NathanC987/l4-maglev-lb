#!/usr/bin/env bash
# Thin wrapper around tools/maglev_remap_report (built as part of the normal
# CMake build - see CMakeLists.txt). Needs no root, no topology, no running
# maglev-lb: purely analytical, over the real maglev_table_generate()
# implementation. Reports Maglev's minimal-disruption property against a
# naive `hash % N` baseline for this benchmark scenario's actual
# transitions (crash: 8->7, scale-out: 5->8 - see scripts/bench/demo), plus
# a sweep showing the ~independent-of-N relationship. See docs/benchmarking.md.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
BIN="${1:-${MAGLEV_REMAP_REPORT_BIN:-$REPO_ROOT/build/maglev_remap_report}}"

if [[ ! -x "$BIN" ]]; then
    echo "maglev_remap_report binary not found at $BIN (build it first: cmake --build build)" >&2
    exit 1
fi

echo "=== Crash scenario: 8 backends -> 7 (one removed) ==="
"$BIN" --before 8 --after 7
echo ""
echo "=== Scale-out scenario: 5 backends -> 8 (three added) ==="
"$BIN" --before 5 --after 8
echo ""
echo "=== Sweep: removing exactly one backend, N=2..16 ==="
"$BIN" --sweep 2 16
echo ""
echo "Maglev's remapped fraction stays far below naive modulo's across every N -"
echo "this is the algorithmic case for Maglev over the simplest possible"
echo "alternative, independent of any live traffic. See docs/benchmarking.md."
