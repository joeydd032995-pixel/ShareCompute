# T1 — llama.cpp RPC, actually run

The first execution-layer check in this project that **runs** rather than mirrors. MLX is Apple-only,
so `Patches/mlx/tests/` reproduces the patched logic against real primitives without ever compiling
MLX. llama.cpp builds on x86_64 Linux, so this drives the real binaries.

`bash Spikes/llamacpp-rpc/run.sh`

## What it establishes

**A. RPC genuinely splits a model across processes.** Two `ggml-rpc-server` processes, one
`llama-cli` client, layers distributed between them — 26 to `RPC0` and 24 to `RPC1` for
Qwen2.5-0.5B-Instruct. Generation completes and exits cleanly. The topology this project needs is
real, not aspirational.

**B. Killing one peer mid-generation aborts the client, in about 350 ms.** Not a hang. Reproducible
3/3:

```text
ggml-rpc.cpp:509: Remote RPC server crashed or returned malformed response
E send failed (bytes_sent=0, size_to_send=8)
```

`:509` and `:519` both appear, depending on whether `set_tensor` or `get_tensor` was in flight.
Both are `RPC_STATUS_ASSERT`, which is one line:

```c
ggml-rpc.cpp:30:  #define RPC_STATUS_ASSERT(x) if (!(x)) GGML_ABORT("Remote RPC server crashed or returned malformed response")
```

18 call sites across 16 functions. Full analysis in `findings.md` **F25**.

## Why the abort matters more than the hang

`abort()` cannot be caught. A host application gets no exception, no error return, no chance to
re-form the ring — the process is simply gone, along with every other node's session state held in
it. Detection is not the problem here; llama.cpp detects a dead peer promptly and correctly. What it
does next is unrecoverable by construction.

That inverts the Phase 4 ordering. F15 listed bounded waits (4.1) before error returns (4.2) on the
assumption that hanging was the primary failure. On this evidence 4.2 comes first: a hang is at least
survivable by a supervisor, whereas an abort takes the whole process down before any supervisor can
act.

## What the transport costs — `throughput.sh` (F27)

`bash Spikes/llamacpp-rpc/throughput.sh`

T1 answered *does it work* and *how does it fail*. Neither answers *is it fast enough*, which is the
question the product rests on. Qwen2.5-0.5B-Instruct Q4_K_M, 128 tokens, 3 repeats, median, on a
4-core Xeon with **no GPU**:

| configuration | PP t/s | TG t/s | wall ms | peers |
|---|---|---|---|---|
| local, no RPC | **217.3** | 24.9 | 6239 | — |
| 1 peer, loopback | 112.5 | 27.6 | 7148 | 1/1 |
| 2 peers, loopback | 117.7 | 27.4 | 7453 | **2/2** (26 + 24 layers) |

The `peers` column is not decoration. An earlier version of this table reported a two-peer row that
was really **one** peer: the second server failed to bind and stayed alive, so llama.cpp registered a
single device and put all 50 tensors on it. The harness now counts distinct devices and refuses the
row unless every endpoint is used. See F27.

**Prompt processing halves (−48%) with no network involved at all.** That is pure transport and
serialisation cost — and it is the metric where `Apps/InferRing/README.md` measures the MLX path
*gaining* 11% on Mac + iPhone. Opposite sign, which is the interesting part.

**A second peer is free.** 112.5 → 117.7 t/s going from one peer to a genuine 26 + 24 split. The cost
is the first hop, not the number of hops. (This conclusion happens to survive the correction above,
but it had no supporting evidence until the split was actually verified.)

**Do not read the TG column as a transport win.** Generation is *higher* under RPC (26.6–28.0 vs
24.5–25.1) because both processes share one 4-core box: with `-ngl 99` every layer moves to the RPC
device, so the server's threadpool does the matmuls while the client blocks on the socket. The
honest reading is that per-token transport cost is below this host's measurement floor, not that it
is negative.

## What T2 has to measure

T2 — a second machine as an RPC node — was scoped to prove discovery, firewall traversal and a real
network hop. That is necessary and **not sufficient**.

**Configuration: two PCs.** The Apple path is out of the near-term plan — no Mac means no Xcode,
which means no iOS build regardless of any developer-account question. The MLX work stays in CI as
regression protection for anyone who does have two Macs; it is not on the critical path. So the ring
is **PC A + PC B**, both x86, no signing, no store, no Apple anything.

### Getting the binaries — probably no build required

llama.cpp's release workflow sets `-DGGML_RPC=ON -DLLAMA_BUILD_TOOLS=ON` globally and packages the
**entire** `build\bin\Release\` directory into `llama-bin-win-cpu-x64.zip`. `ggml-rpc-server` lives
under `tools/rpc/`, so it should be in that zip already.

**Verify rather than assume** — list the archive and look for `ggml-rpc-server.exe`. If it is
missing, build with:

```
cmake -B build -DGGML_RPC=ON -DLLAMA_BUILD_TOOLS=ON
cmake --build build --config Release
```

### Which machine does what

| | role | needs |
|---|---|---|
| **PC A** (server) | holds layers, runs `ggml-rpc-server` | just the binary — no bash, no scripts |
| **PC B** (client) | runs the benchmark and a local worker | the binary **and** bash, so WSL2 or Git Bash if it is Windows |

Only the client side runs `throughput.sh`. If the old laptop is the one you would rather put Linux
on, make it PC B and the scripting problem disappears.

**Only PC B needs the GGUF.** `ggml-rpc-server --help` shows no model argument at all — its entire
option set is `-t/--threads`, `-d/--device`, `-H/--host`, `-p/--port`, `-c/--cache`. The client reads
the model and uploads tensors over the wire. Adding `-c` on the server caches what it receives, which
makes repeat runs much faster; leave it off for the first run so the wall-clock column reflects a
genuine cold upload.

### The one flag that will otherwise ruin the measurement

**`ggml-rpc-server` defaults to `-t 2`.** Two threads. Left alone, PC A computes its share of the
layers on two cores, the split row looks terrible, and you conclude the network is slow when you have
actually measured thread starvation. Set it to PC A's physical core count:

```
.\ggml-rpc-server.exe -H <a-ip> -p 50052 -t 8      # 8 = PC A's cores
```

This is the same class of mistake as everything else recorded in `findings.md`: a number that looks
like an answer to the question you asked, and is an answer to a different one.

### Read this before starting a server on your network

**`ggml-rpc-server` has no authentication, no token and no TLS** — F15 recorded it, and upstream's
own documentation warns never to expose it on an open network. It accepts tensor data and buffer
allocations from anyone who can reach the port. Binding it to `0.0.0.0` publishes that to every
device on your LAN, guest Wi-Fi and all.

So bind it to **one specific interface** and firewall it to **one specific client**:

```powershell
# PC A, the server. Substitute the real addresses -- do not use 0.0.0.0.
#   <a-ip>  this machine's LAN address  (ipconfig)
#   <b-ip>  PC B, the benchmark client, and nothing else
New-NetFirewallRule -DisplayName "ggml-rpc T2" -Direction Inbound -Protocol TCP `
  -LocalPort 50052 -RemoteAddress <b-ip> -Action Allow

.\ggml-rpc-server.exe -H <a-ip> -p 50052
```

```bash
# PC B, the client. Under WSL2 or Git Bash if this machine is Windows.
RPC_ENDPOINTS=<a-ip>:50052 bash Spikes/llamacpp-rpc/throughput.sh
```

Sanity-check reachability first — `Test-NetConnection <a-ip> -Port 50052` from PC B. A firewall
silently dropping the connection looks identical to a peer that is simply slow, and the harness
would report `0/1 peers` without telling you which.

**Stop the server and remove the rule when the run finishes** — this is a measurement, not a
service:

```powershell
Remove-NetFirewallRule -DisplayName "ggml-rpc T2"
```

Treat the link as trusted-network-only until Phase 3.2 adds the shared-secret HMAC the control plane
still lacks. That item is already on the plan for exactly this reason.

### What the run produces

Three rows, and the third is the one that answers T2:

| row | what it measures |
|---|---|
| `local, no RPC` | control, on whatever hardware runs the script |
| `remote only` | **not** pooling — `-ngl 99` puts every layer on the sole remote device, so this is ordinary remote inference and conflates the peer's CPU speed with network cost |
| `split: local + remote` | the actual topology — a local worker *and* the remote peer, so the scheduler divides layers between them and per-token traffic genuinely crosses the link |

The script starts the local worker itself, bound to `127.0.0.1`, so nothing extra is exposed. The
`remote only` row is kept because the two together let you separate the peer's compute speed from
the transport cost, which a single number cannot.

**PP is the leading indicator.** It is already the transport-sensitive metric on loopback, so it is
where a real link will hurt first and hardest. Watch wall clock too — it carries the model upload,
which the t/s figures deliberately exclude and which a slow link punishes most.

This matters because the encouraging −12% / +11% benchmark in `Apps/InferRing/README.md` comes with
a warning two lines above it — *"Wi-Fi and pre-TB5 over RDMA connections will result in sharp
performance decline"* — and a recommendation to use a USB3.2 cable. An iPhone and a Windows PC have
no such cable between them. **The published numbers come from the one configuration this project's
target cannot use.**

## Two ways to get a false reading

Both were hit before the numbers above were trusted, and the script guards against both:

- **`-st` and closed stdin.** Without them `llama-cli` enters conversation mode and waits at a `>`
  prompt after generating. The bounded run then times out and looks *exactly* like a hang — case A
  was initially read as "RPC hangs" when it had in fact generated correctly and was waiting for
  input.
- **`--ignore-eos` and a long `-n`.** A small model reaches end-of-text in about a second, so the
  kill lands after the run is already finished and reports a meaningless "clean exit". The script
  waits for real output before killing, and reports "window too short" rather than a verdict when
  the client finishes first.

## Setup

```bash
git clone --depth=1 https://github.com/ggml-org/llama.cpp /home/user/llama.cpp
cd /home/user/llama.cpp
cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DGGML_RPC=ON -DLLAMA_CURL=OFF -DGGML_NATIVE=OFF
ninja -C build ggml-rpc-server llama-cli

mkdir -p /home/user/models && cd /home/user/models
curl -sSLO https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf
```

The server target is **`ggml-rpc-server`**, under `tools/rpc/`. F15 and F21 call it `rpc-server`
under `examples/rpc/`; it has been renamed and moved since. Override `BIN`, `MODEL`, `OUT` and
`REPEATS` by environment.

Read at llama.cpp `9d57ce4`.

## Not established

No cross-machine hop — both peers are loopback, so nothing here exercises real network latency,
MTU, or a firewall. No iOS. No re-formation after the abort, because there is nothing left to
re-form from. And the abort is observed only for a *hard* kill; a peer that goes away gracefully, or
a network that drops without closing the socket, are separate cases and are exactly where the
missing `SO_RCVTIMEO`/`SO_SNDTIMEO` (still zero occurrences at HEAD) would bite instead.
