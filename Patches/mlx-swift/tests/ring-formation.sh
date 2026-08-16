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

# PROBE_BIN exists so this launcher's own logic -- the watchdog, the exit-code classification, the
# three-way verdict -- can be exercised against a stub on a machine with no MLX. Without it the
# script could only ever be tested by a macOS CI run, which is how it shipped reporting
# "no loopback ring" for a missing timeout(1).
if [ -n "${PROBE_BIN:-}" ]; then
    BIN="$PROBE_BIN"
    echo "==> using PROBE_BIN=$BIN (skipping build)"
else
    echo "==> building RingFormationProbe ($CONFIG)"
    if ! swift build -c "$CONFIG" --product RingFormationProbe; then
        echo "build failed" >&2
        exit 1
    fi
    BIN="$(swift build -c "$CONFIG" --show-bin-path)/RingFormationProbe"
fi
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
        "$BIN" > "$OUT/rank-$r.log" 2>&1 &
    pids="$pids $!"
    r=$((r + 1))
done

# macOS ships no timeout(1) -- it is GNU coreutils, present only as `gtimeout` and only if someone
# installed it. The first run of this job died on `timeout: command not found`, so the bound is now
# a plain watchdog that needs nothing but bash.
#
# One watchdog for the whole set rather than one per rank: the ranks are a unit, and if any of them
# is still alive at the deadline the ring did not close.
TIMED_OUT_MARKER="$OUT/timed-out"
rm -f "$TIMED_OUT_MARKER"
(
    sleep "$TIMEOUT"
    for p in $pids; do
        if kill -0 "$p" 2>/dev/null; then
            : > "$TIMED_OUT_MARKER"
            kill -9 "$p" 2>/dev/null
        fi
    done
) &
WATCHDOG=$!

# The probe's exit codes. Anything outside its own vocabulary means the experiment did not run, and
# that must NOT be reported as a result -- the first version mapped `timeout: command not found`
# (exit 127) onto "no loopback ring", which confidently confirmed the status quo without testing
# anything. A false negative in the direction of an existing belief is the worst kind.
describe() {
    case "$1" in
        0)   echo "OK -- ring formed, allGather correct, finalize refused-then-succeeded" ;;
        10)  echo "NO RING -- ring::init declined, EmptyGroup built" ;;
        11)  echo "WRONG SHAPE -- a group formed but not the requested size/rank" ;;
        12)  echo "COLLECTIVE FAILED -- allGather returned the wrong data" ;;
        13)  echo "FINALIZE WRONG -- teardown did not behave as Stage 2 requires" ;;
        14)  echo "BAD ENVIRONMENT -- the probe could not start; NOT a result" ;;
        124) echo "HANG -- killed after ${TIMEOUT}s (the failure this project exists to kill)" ;;
        127) echo "COMMAND NOT FOUND -- the probe never ran; NOT a result" ;;
        134) echo "ABORT (SIGABRT)" ;;
        139) echo "SEGFAULT" ;;
        # Only 129..164 are plausibly deaths by signal. A bare 255 is not "signal 127" -- it is
        # usually a library aborting after printing its own error, which the rank log will show.
        *)   if [ "$1" -gt 128 ] && [ "$1" -lt 165 ]; then
                 echo "signal $(( $1 - 128 )) -- died without reporting; NOT a result"
             else
                 echo "exit $1 -- died without reporting; NOT a result"
             fi ;;
    esac
}

# Did this rank actually answer the question, whatever the answer was?
#
# 0 and 10-13 are the probe speaking. 124 counts too: a rank that started, tried to reach its peer
# and blocked has told us something real about ring formation -- it is the exact failure this whole
# project exists to kill, so calling it "never ran" would throw away the most interesting negative
# available.
#
# 14/127 mean the probe never got far enough to have an opinion. 134/139 (abort, segfault) are left
# here deliberately: a crash is more likely a defect in the probe than a statement about rings, and
# claiming an answer from one would be exactly the overreach this function exists to prevent.
answered() {
    case "$1" in
        0|10|11|12|13|124) return 0 ;;
        *)                 return 1 ;;
    esac
}

failed=0        # any rank that did not report OK
inconclusive=0  # any rank that never ran, so no conclusion may be drawn at all
r=0
for pid in $pids; do
    wait "$pid"; rc=$?
    # The watchdog kills with SIGKILL, so a timed-out rank shows 137. Normalise it to 124 so the
    # HANG case reads the same as it would under timeout(1).
    if [ "$rc" -eq 137 ] && [ -f "$TIMED_OUT_MARKER" ]; then rc=124; fi
    printf '  rank %d: %s\n' "$r" "$(describe "$rc")"
    [ "$rc" -ne 0 ] && failed=1
    answered "$rc" || inconclusive=1
    r=$((r + 1))
done
kill -9 "$WATCHDOG" 2>/dev/null

echo
echo "==> rank output"
r=0
while [ "$r" -lt "$RANKS" ]; do
    echo "--- rank $r ---"
    cat "$OUT/rank-$r.log"
    r=$((r + 1))
done

echo
# Three outcomes, not two. Collapsing "the experiment could not run" into "the experiment said no"
# is how the first version of this script reported CLAUDE.md's claim confirmed by a missing
# timeout(1).
if [ "$inconclusive" -eq 1 ]; then
    echo "RESULT: INCONCLUSIVE -- at least one rank never ran, so nothing was tested."
    echo "        Draw NO conclusion about whether a loopback ring forms. Fix the launcher"
    echo "        and run again; the per-rank reasons are above."
elif [ "$failed" -eq 0 ]; then
    echo "RESULT: a $RANKS-rank MLX ring formed on one machine and exchanged data."
    echo "        CLAUDE.md's 'needs two or more real devices' is too strong -- it needs two"
    echo "        or more RANKS. Milestone 2's unrun half is testable in CI."
else
    echo "RESULT: the ranks ran and the ring did not form as required."
    echo "        This IS a result: CLAUDE.md's claim stands, and the unrun half of Milestone 2"
    echo "        still needs real hardware. The specific assertion that failed is above."
fi
exit "$failed"
