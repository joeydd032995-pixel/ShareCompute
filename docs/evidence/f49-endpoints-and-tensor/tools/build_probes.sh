#!/bin/bash
# Build the F49 scratch probe from native/split/probe.cpp + probe-f49.diff, with the repository probe's own
# compile flags and link line (read from the existing desktop build). Nothing in the repository is modified.
# usage: SCRATCH=/some/empty/dir tools/build_probes.sh      (needs build/desktop from build_split_runtime.py)
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$HERE/../../../.." && pwd)
SCRATCH=${SCRATCH:?set SCRATCH to a writable directory}
BUILD=${BUILD:-$REPO/build/desktop}
T=$BUILD/CMakeFiles/sc-split-probe.dir
[ -f "$T/flags.make" ] || { echo "no $T/flags.make: build the desktop runtime first" >&2; exit 2; }
mkdir -p "$SCRATCH/bin" "$SCRATCH/src"
patch -s -o "$SCRATCH/src/probe-f49.cpp" "$REPO/native/split/probe.cpp" "$HERE/probe-f49.diff"
DEFS=$(sed -n 's/^CXX_DEFINES = //p' "$T/flags.make"); INCS=$(sed -n 's/^CXX_INCLUDES = //p' "$T/flags.make")
FLAGS=$(sed -n 's/^CXX_FLAGS = //p' "$T/flags.make"); LIBS=$(sed -n 's/.* -o bin\/sc-split-probe *//p' "$T/link.txt")
cd "$BUILD"
eval c++ $FLAGS $DEFS $INCS -c "$SCRATCH/src/probe-f49.cpp" -o "$SCRATCH/src/probe-f49.o"
eval c++ -O3 -DNDEBUG "$SCRATCH/src/probe-f49.o" -o "$SCRATCH/bin/probe-f49" $LIBS
echo "built $SCRATCH/bin/probe-f49"
