#!/usr/bin/env bash
#
# T1 — does llama.cpp RPC actually work across processes, and how does it fail?
#
# Unlike every execution-layer check before it, this one RUNS. MLX is Apple-only, so its harnesses
# mirror the patched code; llama.cpp builds and runs on x86_64 Linux, so this drives the real thing.
#
# Two cases:
#   A. Two rpc-servers, one client, one generation — are layers genuinely split?
#   B. Kill one server mid-generation — hang, abort, or clean error?
#
# B is the point. It is the portable-path analogue of MLX Spike B, and its answer decides the
# ordering of Phase 4. See findings.md F25.
#
#   bash Spikes/llamacpp-rpc/run.sh                 # both cases
#   REPEATS=5 bash Spikes/llamacpp-rpc/run.sh       # case B more times, for reproducibility
#
# Requires llama.cpp built with -DGGML_RPC=ON, and a GGUF. See README.md in this directory.

set -uo pipefail

BIN=${BIN:-/home/user/llama.cpp/build/bin}
MODEL=${MODEL:-/home/user/models/qwen2.5-0.5b-instruct-q4_k_m.gguf}
OUT=${OUT:-/tmp/t1-rpc}
REPEATS=${REPEATS:-3}

for f in "$BIN/ggml-rpc-server" "$BIN/llama-cli"; do
    [ -x "$f" ] || { echo "missing $f — see README.md" >&2; exit 1; }
done
[ -r "$MODEL" ] || { echo "missing model $MODEL — see README.md" >&2; exit 1; }

rm -rf "$OUT" && mkdir -p "$OUT"
export LD_LIBRARY_PATH="$BIN:${LD_LIBRARY_PATH:-}"

banner() { printf '\n\033[1m===== %s\033[0m\n' "$*"; }

# `-st` and closed stdin are both load-bearing. Without them llama-cli enters conversation mode and
# waits at a prompt after generating, which times out and looks exactly like a hang. That cost one
# wrong reading of case A before it was spotted.
CLI_COMMON=(-ngl 99 -c 8192 -st -no-cnv)

serve() { "$BIN/ggml-rpc-server" -H 127.0.0.1 -p "$1" > "$OUT/srv-$2.log" 2>&1 & echo $!; }

# 124 is timeout(1) reporting that it killed a hang; >128 is death by signal N-128, so 134 = SIGABRT.
verdict() {
    case "$1" in
        0)   echo "clean exit" ;;
        124) echo "HANG (timeout killed it)" ;;
        134) echo "ABORT (SIGABRT — uncatchable)" ;;
        139) echo "SEGFAULT" ;;
        *)   if [ "$1" -gt 128 ]; then echo "signal $(( $1 - 128 ))"; else echo "error exit $1"; fi ;;
    esac
}

banner "A. Two peers, one generation — are layers split?"
PA=$(serve 50300 a); PB=$(serve 50301 b); sleep 2
timeout 120 "$BIN/llama-cli" -m "$MODEL" --rpc 127.0.0.1:50300,127.0.0.1:50301 \
    "${CLI_COMMON[@]}" -n 64 -v -p "Explain gravity briefly." < /dev/null > "$OUT/a.log" 2>&1
echo "  $(verdict $?)"
grep -oE "assigned to device RPC[0-9]+" "$OUT/a.log" | sort | uniq -c | sed 's/^/    /'
kill -9 $PA $PB 2>/dev/null; sleep 1

banner "B. Kill one peer mid-generation — $REPEATS runs"
for r in $(seq 1 "$REPEATS"); do
    pa=$((50310 + r*2)); pb=$((50311 + r*2))
    PA=$(serve $pa "k${r}a"); PB=$(serve $pb "k${r}b"); sleep 2
    log="$OUT/b$r.log"
    # --ignore-eos and a large -n so generation lasts long enough to interrupt. Without it a small
    # model reaches EOS in about a second and the kill lands after the run is already over, which
    # reports a meaningless "clean exit".
    timeout 120 "$BIN/llama-cli" -m "$MODEL" --rpc 127.0.0.1:$pa,127.0.0.1:$pb \
        "${CLI_COMMON[@]}" -n 100000 --ignore-eos \
        -p "Write a long detailed essay about the ocean." < /dev/null > "$log" 2>&1 &
    client=$!
    # Wait for real output before killing, so the peer dies mid-generation rather than mid-load.
    for _ in $(seq 1 60); do
        sleep 0.5
        kill -0 $client 2>/dev/null || break
        [ "$(wc -c < "$log")" -gt 1500 ] && break
    done
    if kill -0 $client 2>/dev/null; then
        t0=$(date +%s%3N); kill -9 $PB 2>/dev/null
        wait $client; rc=$?; dt=$(( $(date +%s%3N) - t0 ))
        printf '  run %d: %-28s in %5d ms  |  %s\n' "$r" "$(verdict $rc)" "$dt" \
            "$(grep -oE 'ggml-rpc\.cpp:[0-9]+: .*' "$log" | head -1 | cut -c1-64)"
    else
        printf '  run %d: client finished before the kill — window too short, not a result\n' "$r"
    fi
    kill -9 $PA $PB 2>/dev/null; sleep 1
done

echo; echo "logs in $OUT"
