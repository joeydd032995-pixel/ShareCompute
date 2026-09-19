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
#   BIN=/home/user/bin-pr bash Spikes/llamacpp-rpc/run.sh    # against a different build
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
#
# `-v` is equally load-bearing but for a different reason: without it the log carries no
# "assigned to device RPC0" lines, and there is then no way to tell a genuine two-peer run from one
# where a server never bound and every tensor landed on the survivor. Killing the unused peer of
# such a run produces a confident "clean exit" that means nothing. F27 hit exactly that in
# throughput.sh and published a one-peer row as a two-peer result.
CLI_COMMON=(-ngl 99 -c 8192 -st -no-cnv -v)

# Ports are derived from the PID so a rerun cannot collide with sockets still in TIME_WAIT from the
# previous one, and they sit BELOW the ephemeral range so they cannot collide with this run either.
#
# That second constraint was learned the hard way. The first version based ports at 50000, which is
# inside Linux's default ip_local_port_range (32768-60999) — so llama-cli's own outbound RPC
# connections could transiently occupy the very port a later server tried to bind, and one run in
# three died with "Failed to create server socket". An intermittent bind failure is the worst
# possible shape for this bug: it produces a silent one-peer run, which is exactly the false result
# F27 was written to stop.
EPHEMERAL_LO=$(awk '{print $1}' /proc/sys/net/ipv4/ip_local_port_range 2>/dev/null || echo 32768)
PORT_BASE=$(( 20000 + (($$ * 7) % 10000) ))
if [ "$(( PORT_BASE + 64 ))" -ge "$EPHEMERAL_LO" ]; then
    PORT_BASE=$(( EPHEMERAL_LO > 5000 ? EPHEMERAL_LO - 5000 : 10000 ))
fi
PORT_NEXT=$PORT_BASE

# Start a server AND prove it is listening.
#
# Two failure modes, and neither is caught by checking the PID. `sleep 2` and hope is what produced
# the bad row in F27: the process starts, prints its banner, fails to bind, and stays alive — so a
# PID check passes while nothing is served. And a bind failure is worth retrying on a fresh port
# rather than aborting, because the usual cause is a transient ephemeral-port collision.
#
# SERVE_PID/SERVE_PORT are globals rather than stdout on purpose. The obvious `pid=$(serve ...)`
# runs the function in a subshell, where `exit 1` kills only that subshell and the script sails on
# with an empty PID — the guard fires, prints, and changes nothing. throughput.sh still had that
# shape when this was found.
SERVE_PID=""; SERVE_PORT=""
serve() { # <tag> -> sets SERVE_PID and SERVE_PORT, or exits the script
    local tag=$1 log="$OUT/srv-$1.log" port pid attempt _
    for attempt in 1 2 3 4 5; do
        port=$PORT_NEXT; PORT_NEXT=$((PORT_NEXT + 1))
        : > "$log"
        "$BIN/ggml-rpc-server" -H 127.0.0.1 -p "$port" > "$log" 2>&1 &
        pid=$!
        for _ in $(seq 1 40); do
            if grep -q "Failed to create server socket" "$log" 2>/dev/null; then
                echo "  (server $tag could not bind 127.0.0.1:$port, retrying on $PORT_NEXT)" >&2
                kill -9 "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
                pid=""; break
            fi
            # /dev/tcp is a bash builtin, so this needs no extra tooling on the runner.
            (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null && {
                exec 3<&- 3>&-; SERVE_PID=$pid; SERVE_PORT=$port; return 0; }
            sleep 0.25
        done
        [ -n "$pid" ] && { kill -9 "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; }
    done
    echo "server $tag never listened after 5 attempts — see $log" >&2
    exit 1
}

# DISTINCT devices, not the tensor count. With two endpoints and one of them dead, every tensor
# lands on the survivor and the total still looks healthy — only the distinct count separates a
# split ring from a single node.
ndev() { grep -oE 'assigned to device RPC[0-9]+' "$1" 2>/dev/null | sort -u | wc -l | tr -d ' '; }

# Bytes of GENERATED text — everything on stdout after the echoed prompt.
#
# A plain size check on stdout does not work, and looking like it works is the trap: llama-cli
# writes a spinner, an ASCII banner, a build/model block and a command list before it echoes the
# prompt, which is ~1300 bytes of stdout with not one token generated. Any threshold below that
# fires during model load, so the peer dies mid-upload and the run exercises the tensor-transfer
# path rather than the per-token one this case is about. Stripping through the echoed prompt is
# what makes the number mean tokens.
BPROMPT="Write a long detailed essay about the ocean."
genlen() { # <stdout file> -> bytes generated after the prompt echo
    local s after
    s=$(tr -d '\0' < "$1" 2>/dev/null) || { echo 0; return; }
    after=${s##*"$BPROMPT"}
    [ "$after" = "$s" ] && { echo 0; return; }   # prompt not echoed yet — still loading
    echo "${#after}"
}

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

# Both message forms. Unpatched llama.cpp aborts through RPC_STATUS_ASSERT and prints
#   ggml-rpc.cpp:509: Remote RPC server crashed or returned malformed response
# ggml-org/llama.cpp#26724 replaces that with a logged error and a status return,
#   [func] RPC server 127.0.0.1:NNNNN crashed or returned a malformed response - the device is ...
# A regex matching only the first reports an empty diagnostic for the second, which would read as
# "no error message" when the truth is "a different error message".
diag() {
    grep -ohE 'ggml-rpc\.cpp:[0-9]+: [^\n]*|crashed or returned a? ?malformed response[^\n]*|graph computation failed[^\n]*' \
        "$@" 2>/dev/null | head -1 | cut -c1-72
}

banner "A. Two peers, one generation — are layers split?"
serve a; PA=$SERVE_PID; pa=$SERVE_PORT
serve b; PB=$SERVE_PID; pb=$SERVE_PORT
timeout 180 "$BIN/llama-cli" -m "$MODEL" --rpc 127.0.0.1:$pa,127.0.0.1:$pb \
    "${CLI_COMMON[@]}" -n 64 -p "Explain gravity briefly." \
    < /dev/null > "$OUT/a.out" 2> "$OUT/a.log"
rc=$?
echo "  $(verdict $rc)"
grep -oE "assigned to device RPC[0-9]+" "$OUT/a.log" | sort | uniq -c | sed 's/^/    /'
a_nd=$(ndev "$OUT/a.log")
if [ "$a_nd" -lt 2 ]; then
    echo "  INVALID: $a_nd distinct RPC device(s), expected 2 — case B cannot be trusted either" >&2
    kill -9 $PA $PB 2>/dev/null
    exit 1
fi
echo "    -> $a_nd distinct RPC devices, split confirmed"
kill -9 $PA $PB 2>/dev/null; sleep 1

banner "B. Kill one peer mid-generation — $REPEATS runs"
aborts=0; hangs=0; errors=0; cleans=0; invalid=0
for r in $(seq 1 "$REPEATS"); do
    serve "k${r}a"; PA=$SERVE_PID; pa=$SERVE_PORT
    serve "k${r}b"; PB=$SERVE_PID; pb=$SERVE_PORT
    log="$OUT/b$r.log"; out="$OUT/b$r.out"
    : > "$out"
    # --ignore-eos and a large -n so generation lasts long enough to interrupt. Without it a small
    # model reaches EOS in about a second and the kill lands after the run is already over, which
    # reports a meaningless "clean exit".
    timeout 180 "$BIN/llama-cli" -m "$MODEL" --rpc 127.0.0.1:$pa,127.0.0.1:$pb \
        "${CLI_COMMON[@]}" -n 100000 --ignore-eos \
        -p "$BPROMPT" < /dev/null > "$out" 2> "$log" &
    client=$!
    # Two independent conditions, and both must hold before the kill means anything:
    #
    #   1. both peers are genuinely in use  — read from the verbose log (stderr)
    #   2. generation has actually started  — read from the generated text (stdout)
    #
    # Splitting the streams is what makes 2 checkable at all, and genlen() is what makes it
    # honest: it counts only what follows the echoed prompt, so the banner and the model-load
    # chatter cannot be mistaken for tokens. 16 bytes is a handful of tokens — enough that decode
    # is demonstrably running and the per-token collective is in flight.
    peers_ok=0; gen_ok=0
    for _ in $(seq 1 360); do
        sleep 0.5
        kill -0 $client 2>/dev/null || break
        [ "$peers_ok" -eq 0 ] && [ "$(ndev "$log")" -ge 2 ] && peers_ok=1
        [ "$(genlen "$out")" -gt 16 ] && gen_ok=1
        [ "$peers_ok" -eq 1 ] && [ "$gen_ok" -eq 1 ] && break
    done
    if ! kill -0 $client 2>/dev/null; then
        printf '  run %d: client finished before the kill — window too short, not a result\n' "$r"
        invalid=$((invalid+1))
    elif [ "$peers_ok" -eq 0 ]; then
        printf '  run %d: INVALID — only %s distinct peer(s) in use; killing one proves nothing\n' \
            "$r" "$(ndev "$log")"
        invalid=$((invalid+1)); kill -9 $client 2>/dev/null; wait $client 2>/dev/null
    elif [ "$gen_ok" -eq 0 ]; then
        printf '  run %d: INVALID — generation never started (%s bytes after the prompt echo)\n' \
            "$r" "$(genlen "$out")"
        invalid=$((invalid+1)); kill -9 $client 2>/dev/null; wait $client 2>/dev/null
    else
        gen_at=$(genlen "$out")
        t0=$(date +%s%3N); kill -9 $PB 2>/dev/null
        wait $client; rc=$?; dt=$(( $(date +%s%3N) - t0 ))
        case "$rc" in
            134) aborts=$((aborts+1)) ;;
            124) hangs=$((hangs+1)) ;;
            0)   cleans=$((cleans+1)) ;;
            *)   errors=$((errors+1)) ;;
        esac
        printf '  run %d: %-28s in %5d ms  (killed after %s gen bytes)\n            %s\n' \
            "$r" "$(verdict $rc)" "$dt" "$gen_at" "$(diag "$log" "$out")"
    fi
    kill -9 $PA $PB 2>/dev/null; sleep 1
done

# Report the spread, not just the last line. A mixed 2-abort/1-error result is a different finding
# than a uniform one, and reading it off three scrolled lines is how a partial result gets written
# up as a clean one.
banner "B summary"
printf '  aborts=%d  hangs=%d  error-exits=%d  clean-exits=%d  invalid=%d  (of %d)\n' \
    "$aborts" "$hangs" "$errors" "$cleans" "$invalid" "$REPEATS"
if [ "$invalid" -gt 0 ]; then
    echo "  NOTE: $invalid run(s) produced no verdict — do not quote a rate over $REPEATS."
fi

echo; echo "logs in $OUT  (.log = verbose stderr, .out = generated text)"
