#!/usr/bin/env bash
#
# Form a real MLX ring out of two processes on one machine, and prove it works.
#
# This is the experiment that decides whether `CLAUDE.md`'s standing claim -- that a multi-device
# ring "needs two or more real devices" -- is true, or whether it has been conflating *two ranks*
# with *two machines* since the first session. See the header of RingFormationProbe/main.swift for
# the source reading that motivates it.
#
# If a loopback ring forms, the whole "unrun" half of Milestone 2 becomes testable on a free CI
# runner: Stage 1's fail-instead-of-hang path, Stage 2's finalize() against a group that genuinely
# exists, and the per-token allGather barrier. None of that has ever executed.
#
#   bash Patches/mlx-swift/tests/ring-formation.sh
#   RANKS=3 bash Patches/mlx-swift/tests/ring-formation.sh
#
# Apple-only: MLX does not build on Linux. Expects to be run from the repository root or this
# directory. macOS ships bash 3.2, so nothing here uses bash 4 syntax.

set -uo pipefail

cd "$(dirname "$0")"

RANKS=${RANKS:-2}
CONFIG=${CONFIG:-debug}
# Generous: the first run builds MLX from source. The *ring* itself either forms in seconds or
# blocks forever, so this bound exists to turn a hang into a reported result rather than a job that
# burns its whole allowance -- the same discipline as the MLX C++ harnesses.
TIMEOUT=${TIMEOUT:-180}
OUT=${OUT:-/tmp/mlx-ring-formation}

rm -rf "$OUT" && mkdir -p "$OUT"

echo "==> building RingFormationProbe ($CONFIG)"
if ! swift build -c "$CONFIG" --product RingFormationProbe; then
    echo "build failed" >&2
    exit 1
fi
BIN="$(swift build -c "$CONFIG" --show-bin-path)/RingFormationProbe"
[ -x "$BIN" ] || { echo "probe binary not found at $BIN" >&2; exit 1; }

# Ports derived from the PID so a rerun cannot collide with sockets left in TIME_WAIT by the last
# one. A fixed range caused exactly that failure in the llama.cpp harness (F27), where a server that
# could not bind stayed alive and the run silently measured one peer instead of two.
PORT_BASE=$(( 49000 + ($$ * 7) % 8000 ))

# The hostfile format ring.cpp expects: a JSON array with one entry per rank, each entry a list of
# addresses. One address per rank is enough; multiple would open multiple connections per peer.
HOSTFILE="$OUT/hostfile.json"
{
    printf '['
    r=0
    while [ "$r" -lt "$RANKS" ]; do
        [ "$r" -gt 0 ] && printf ','
        printf '["127.0.0.1:%d"]' $((PORT_BASE + r))
        r=$((r + 1))
    done
    printf ']\n'
} > "$HOSTFILE"

echo "==> hostfile: $(cat "$HOSTFILE")"
echo "==> launching $RANKS ranks"

# Every rank must be launched before any can finish: rank N connects to rank (N+1) % size, so with
# one rank running alone the ring can never close and the probe would time out for the wrong reason.
pids=""
r=0
while [ "$r" -lt "$RANKS" ]; do
    MLX_HOSTFILE="$HOSTFILE" \
    MLX_RANK="$r" \
    PROBE_EXPECT_SIZE="$RANKS" \
    MLX_RING_VERBOSE=1 \
        timeout "$TIMEOUT" "$BIN" > "$OUT/rank-$r.log" 2>&1 &
    pids="$pids $!"
    r=$((r + 1))
done

# The probe's exit codes; anything unlisted is a genuine surprise and says so.
describe() {
    case "$1" in
        0)   echo "OK -- ring formed, allGather correct, finalize refused-then-succeeded" ;;
        10)  echo "NO RING -- ring::init declined, EmptyGroup built" ;;
        11)  echo "WRONG SHAPE -- a group formed but not the requested size/rank" ;;
        12)  echo "COLLECTIVE FAILED -- allGather returned the wrong data" ;;
        13)  echo "FINALIZE WRONG -- teardown did not behave as Stage 2 requires" ;;
        14)  echo "BAD ENVIRONMENT -- launcher error, not a finding" ;;
        124) echo "HANG -- timed out after ${TIMEOUT}s (the failure this project exists to kill)" ;;
        134) echo "ABORT (SIGABRT)" ;;
        139) echo "SEGFAULT" ;;
        *)   if [ "$1" -gt 128 ]; then echo "signal $(( $1 - 128 ))"; else echo "exit $1"; fi ;;
    esac
}

failed=0
r=0
for pid in $pids; do
    wait "$pid"; rc=$?
    printf '  rank %d: %s\n' "$r" "$(describe "$rc")"
    [ "$rc" -ne 0 ] && failed=1
    r=$((r + 1))
done

echo
echo "==> rank output"
r=0
while [ "$r" -lt "$RANKS" ]; do
    echo "--- rank $r ---"
    cat "$OUT/rank-$r.log"
    r=$((r + 1))
done

echo
if [ "$failed" -eq 0 ]; then
    echo "RESULT: a $RANKS-rank MLX ring formed on one machine and exchanged data."
    echo "        CLAUDE.md's 'needs two or more real devices' is too strong -- it needs two"
    echo "        or more RANKS. Milestone 2's unrun half is testable in CI."
else
    echo "RESULT: no loopback ring. CLAUDE.md's claim stands as written; the unrun half of"
    echo "        Milestone 2 still needs real hardware. The per-rank reasons are above."
fi
exit "$failed"
