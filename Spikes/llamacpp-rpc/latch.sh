#!/usr/bin/env bash
#
# T1c — is a failed RPC endpoint recoverable within one process?
#
# F33 established, by reading `ggml-org/llama.cpp#26724`, that the failed-endpoint set is
# insert-only: one `insert`, no `erase`, no `clear`, and `rpc_endpoint_is_failed` is file-static
# with nothing exposed in `ggml-rpc.h`. It could not establish it by RUNNING, because `llama-cli`
# exits as soon as the generation fails and a process-lifetime latch needs a process that lives.
#
# `llama-server` is that process. It stays up, holds the model, and serves request after request,
# so it can be asked the question F33 could not: once an endpoint has failed, can this process ever
# use it again — even after the peer comes back healthy on the same address?
#
#   bash Spikes/llamacpp-rpc/latch.sh
#   BIN=/home/user/bin-pr bash Spikes/llamacpp-rpc/latch.sh
#
# Requires a build of ggml-org/llama.cpp#26724 (or later, if the latch survives). Against the
# UNPATCHED base this experiment cannot run at all: the peer kill aborts the whole process, so
# there is no survivor to interrogate. That asymmetry is the point of the PR.
#
# THE DISCRIMINATOR. "The request still fails" is not enough on its own, because several different
# things would each produce it, and all of them are present at once: the failed-endpoint latch, the
# reg_map registration cached by endpoint string, and the model's buffers on the dead peer simply
# being gone. The PR's author names the last himself -- "a coordinator with a dead endpoint has
# invalid buffers and can't recover".
#
# So the measurement is not the request, it is the RESTARTED PEER'S LOG. ggml-rpc-server prints
# "Accepted client connection" on every connect. Zero connections from llama-server means the
# healthy peer is INVISIBLE to the client -- it is not retrying the address, whatever else is also
# wrong. That is the fact re-formation actually depends on, and it is decidable here.
#
# It does NOT isolate which cache is responsible, and this script does not claim to. The client
# still writes to the stale socket it already holds, so it is not short-circuiting before all
# network activity; separating latch from reg_map from buffers needs a build with one disabled.

set -uo pipefail

BIN=${BIN:-/home/user/bin-pr}
MODEL=${MODEL:-/home/user/models/qwen2.5-0.5b-instruct-q4_k_m.gguf}
OUT=${OUT:-/tmp/t1-latch}

for f in "$BIN/ggml-rpc-server" "$BIN/llama-server"; do
    [ -x "$f" ] || { echo "missing $f — build it: ninja -C build llama-server" >&2; exit 1; }
done
[ -r "$MODEL" ] || { echo "missing model $MODEL — see README.md" >&2; exit 1; }
command -v curl >/dev/null || { echo "need curl" >&2; exit 1; }

rm -rf "$OUT" && mkdir -p "$OUT"
export LD_LIBRARY_PATH="$BIN:${LD_LIBRARY_PATH:-}"

banner() { printf '\n\033[1m===== %s\033[0m\n' "$*"; }
note()   { printf '  %s\n' "$*"; }

# Below the ephemeral range, for the reason F33 records: a port inside ip_local_port_range can be
# taken transiently by the client's own outbound connections, and an intermittent bind failure
# silently produces a result about the wrong topology.
EPHEMERAL_LO=$(awk '{print $1}' /proc/sys/net/ipv4/ip_local_port_range 2>/dev/null || echo 32768)
PORT_BASE=$(( 20000 + (($$ * 7) % 10000) ))
[ "$(( PORT_BASE + 16 ))" -ge "$EPHEMERAL_LO" ] && PORT_BASE=15000
PA=$((PORT_BASE)); PB=$((PORT_BASE + 1)); PS=$((PORT_BASE + 2))

listening() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && { exec 3<&- 3>&-; return 0; }; return 1; }

# Unlike run.sh's serve(), this one must hold an EXACT port: the whole experiment turns on the peer
# coming back at the address the client already knows. A different port would be a different
# endpoint string, which the latch would not match -- and would silently answer a question nobody
# asked. So it retries the same port rather than moving on, and gives up loudly.
serve_exact() { # <port> <logfile> -> pid on stdout, or exits
    local port=$1 log=$2 pid attempt _
    for attempt in 1 2 3 4 5 6 7 8 9 10; do
        : > "$log"
        "$BIN/ggml-rpc-server" -H 127.0.0.1 -p "$port" > "$log" 2>&1 &
        pid=$!
        for _ in $(seq 1 40); do
            grep -q "Failed to create server socket" "$log" 2>/dev/null && break
            listening "$port" && { echo "$pid"; return 0; }
            sleep 0.25
        done
        kill -9 "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
        sleep 1   # a listener does not enter TIME_WAIT, but its accepted connections do
    done
    echo "could not bind 127.0.0.1:$port after 10 attempts — see $log" >&2
    exit 1
}

# Counts "Accepted client connection" — the discriminator described in the header.
#
# This has to be read as a DELTA, not a total, because the harness pollutes it: `listening()` opens
# a real TCP connection with /dev/tcp to prove the port is up, and ggml-rpc-server logs that as an
# accept like any other. The first run of this script reported "2 accepted connections" and
# therefore refused to conclude anything — correctly, but the 2 were both its own probes (each
# logged "Accepted client connection" followed instantly by "Client connection closed", with no
# HELLO and no commands in between, which is exactly what a bare connect-and-close looks like).
#
# So: snapshot after all probing is finished, and attribute only what arrives afterwards.
accepts() { grep -c "Accepted client connection" "$1" 2>/dev/null || echo 0; }

# Returns: <http-status>|<first 90 chars of body, newlines stripped>
ask() { # <tag>
    local tag=$1 body code
    body=$(curl -sS -m 120 -w '\n%{http_code}' -X POST "http://127.0.0.1:$PS/completion" \
        -H 'Content-Type: application/json' \
        -d '{"prompt":"Write one sentence about the ocean.","n_predict":24,"stream":false}' \
        2>"$OUT/curl-$tag.err")
    code=$(printf '%s' "$body" | tail -1)
    printf '%s' "$body" | head -n -1 > "$OUT/resp-$tag.json"
    printf '%s|%s' "${code:-000}" "$(tr -d '\n' < "$OUT/resp-$tag.json" | cut -c1-90)"
}

alive() { kill -0 "$1" 2>/dev/null; }

banner "Setup — two peers, one long-lived llama-server"
APID=$(serve_exact "$PA" "$OUT/peer-a.log");    note "peer A  pid $APID  127.0.0.1:$PA"
BPID=$(serve_exact "$PB" "$OUT/peer-b-1.log");  note "peer B  pid $BPID  127.0.0.1:$PB"

"$BIN/llama-server" -m "$MODEL" --host 127.0.0.1 --port "$PS" \
    --rpc "127.0.0.1:$PA,127.0.0.1:$PB" -ngl 99 -c 4096 -v \
    > "$OUT/server.log" 2>&1 &
SPID=$!
for _ in $(seq 1 240); do
    sleep 0.5
    alive $SPID || { echo "llama-server died during load — see $OUT/server.log" >&2; exit 1; }
    listening "$PS" && [ "$(curl -sS -m 5 "http://127.0.0.1:$PS/health" 2>/dev/null | grep -c ok)" -gt 0 ] && break
done
alive $SPID || { echo "llama-server not alive" >&2; exit 1; }
note "llama-server pid $SPID on 127.0.0.1:$PS"

nd=$(grep -oE 'assigned to device RPC[0-9]+' "$OUT/server.log" 2>/dev/null | sort -u | wc -l | tr -d ' ')
note "distinct RPC devices in use: $nd"
if [ "${nd:-0}" -lt 2 ]; then
    echo "  INVALID: expected 2 distinct RPC devices, got $nd — killing one proves nothing" >&2
    kill -9 $SPID $APID $BPID 2>/dev/null; exit 1
fi

banner "1. Baseline — both peers alive"
r1=$(ask 1); note "HTTP ${r1%%|*}"; note "${r1#*|}"
[ "${r1%%|*}" = "200" ] || { echo "  baseline request failed; the rest is meaningless" >&2; \
    kill -9 $SPID $APID $BPID 2>/dev/null; exit 1; }
a_before=$(accepts "$OUT/peer-b-1.log"); note "peer B accepted $a_before connections so far"

banner "2. Kill peer B"
kill -9 $BPID 2>/dev/null; wait $BPID 2>/dev/null
sleep 1
note "peer B killed; llama-server alive? $(alive $SPID && echo yes || echo NO)"
alive $SPID || { echo "  llama-server died with the peer — this build still aborts" >&2; exit 1; }

r2=$(ask 2); note "HTTP ${r2%%|*}"; note "${r2#*|}"
h2=$(curl -sS -m 5 "http://127.0.0.1:$PS/health" 2>/dev/null | cut -c1-60); note "/health: $h2"

banner "3. Restart peer B on the SAME address, then ask again"
BPID2=$(serve_exact "$PB" "$OUT/peer-b-2.log")
note "peer B back  pid $BPID2  127.0.0.1:$PB  (fresh log: peer-b-2.log)"
listening "$PB" && note "port $PB accepts connections again" || note "port $PB NOT listening"

# Everything above this line that touches peer B is the harness. Snapshot here so the count below
# belongs to llama-server alone.
a_probe=$(accepts "$OUT/peer-b-2.log")
note "harness probe connections to discount: $a_probe"

r3=$(ask 3); note "HTTP ${r3%%|*}"; note "${r3#*|}"
a_total=$(accepts "$OUT/peer-b-2.log")
a_after=$(( a_total - a_probe ))
h3=$(curl -sS -m 5 "http://127.0.0.1:$PS/health" 2>/dev/null | cut -c1-60); note "/health: $h3"

banner "Result"
printf '  baseline (both alive)        HTTP %s\n' "${r1%%|*}"
printf '  after peer B killed          HTTP %s\n' "${r2%%|*}"
printf '  after peer B restarted       HTTP %s\n' "${r3%%|*}"
printf '  connections the RESTARTED peer B accepted from llama-server: %s\n' "$a_after"
printf '    (%s total on that peer, minus %s harness port-probes)\n' "$a_total" "$a_probe"
echo

if [ "${r3%%|*}" = "200" ]; then
    echo "  RECOVERED — the endpoint was reusable after the peer came back."
    echo "  This contradicts F33's reading. Re-read ggml-rpc.cpp before believing it."
elif [ "${a_after:-0}" -eq 0 ]; then
    echo "  NO RECONNECTION — the endpoint is permanently unusable inside this process."
    echo "  The request still fails, and the restarted peer received NOTHING from llama-server."
    echo "  A healthy peer, listening on the address the client already knows, is invisible to it."
    echo
    echo "  What this does NOT say is which of the three caches is responsible. The client does"
    echo "  still write — to the STALE socket it already holds (see 'send failed' in server.log,"
    echo "  bytes_sent=0) — so it is not short-circuiting before all network activity. The failed"
    echo "  latch, add_server's reg_map, and the dead buffers are all present and each alone would"
    echo "  produce this. Isolating them needs a build with one of them disabled."
else
    echo "  RECONNECTED BUT STILL FAILING — the restarted peer accepted $a_after connection(s)"
    echo "  from llama-server, so the endpoint is not invisible; something else broke the request."
    echo "  Read $OUT/server.log before concluding anything about the latch."
fi

echo
echo "  llama-server survived the whole run: $(alive $SPID && echo yes || echo NO)"
kill -9 $SPID $APID $BPID2 2>/dev/null
echo "  logs in $OUT"
