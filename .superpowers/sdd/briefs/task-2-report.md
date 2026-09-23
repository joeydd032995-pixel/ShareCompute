# Task 2 Report: RPC adapter — probe, serve, client, status

## Status
DONE

## TDD evidence

### RED
After adding `test_probe_missing_bin_raises` / `test_parse_cli_status_detects_decode_fail` (and `_fail`) to `scripts/portable_rpc_selftest.py`, before creating the adapter module:

```bash
python3 scripts/portable_rpc_selftest.py
```

```text
PASS: backend constants
PASS: resolve bin/model
PASS: rpc_port_base
PASS: rpc_port_base rejects too-low ephemeral
PASS: rpc_port_base rejects empty ephemeral
Traceback (most recent call last):
  File ".../scripts/portable_rpc_selftest.py", line 154, in <module>
    raise SystemExit(main())
  ...
  File ".../scripts/portable_rpc_selftest.py", line 110, in test_probe_missing_bin_raises
    from portable_dual_topology_rpc import RpcProbeError, probe_llama_bin
ModuleNotFoundError: No module named 'portable_dual_topology_rpc'
```

Exit code: 1 (expected FAIL).

### GREEN
After implementing `scripts/portable_dual_topology_rpc.py`:

```bash
python3 scripts/portable_rpc_selftest.py
python3 scripts/portable_dual_topology_selftest.py
```

```text
PASS: backend constants
PASS: resolve bin/model
PASS: rpc_port_base
PASS: rpc_port_base rejects too-low ephemeral
PASS: rpc_port_base rejects empty ephemeral
PASS: probe missing bin
PASS: parse_cli_logs
ALL PASS: portable_rpc_selftest (units)
...
ALL PASS: portable_dual_topology_selftest
```

Both exit 0.

## Commit
- `2348cb8252242481beb2afe40046270477148091` — `feat(portable-rpc): adapter probe/serve/generate/status`
- Author/committer via env only: `joeydd032995-pixel <joeydd032995-pixel@users.noreply.github.com>`
- Files: `scripts/portable_dual_topology_rpc.py` (new), `scripts/portable_rpc_selftest.py` (tests + `_fail`)
- Not committed: `.superpowers/`, `docs/superpowers/*` scratch, `__pycache__`

## What landed

`scripts/portable_dual_topology_rpc.py`:
- `RpcProbeError`, `RpcRunResult`
- `probe_llama_bin`: requires executable `ggml-rpc-server` + (`llama-server` and/or `llama-cli`); scans BIN file bytes for `#26724` marker `the device is now unusable`
- `start_rpc_server`: loopback `127.0.0.1`, ports from `rpc_port_base(pid)` with up to 5 retries (skips discovery `37777`), proves listen via `socket.create_connection`, sets `LD_LIBRARY_PATH=bin_dir`
- `stop_proc`: SIGTERM then kill
- `parse_cli_logs`: `ok=False` on fail markers (`llama_decode…ret = -3`, `ret = -3`, `Compute error`, `graph computation failed`, `crashed or returned a malformed response`) or empty stdout; never treats `client_rc==0` alone as success
- `run_rpc_generate`: probe + resolve model; start RPC server if not passed; prefer `llama-server` + POST `/completion` (`prompt`/`n_predict`/`stream=false`, fail HTTP ≥500); else `llama-cli` with spike `CLI_COMMON` (`-ngl 99 -c 8192 -st -no-cnv -v`) + `--rpc 127.0.0.1:PORT`, stdin closed

## Self-review

| Check | Result |
|---|---|
| Probe missing BIN raises `RpcProbeError` | Covered by unit test |
| Success never from `llama-cli` exit 0 alone | `parse_cli_logs` requires non-empty stdout and no fail markers |
| ≤1 ggml-rpc-server + 1 client; loopback | `start_rpc_server` one process; client is llama-server or llama-cli |
| Ports below ephemeral / no 37777 | Uses `rpc_port_base`; candidates skip 37777 |
| Prefer llama-server `/completion` | Branch on executable presence |
| Auth/TLS none | No auth added |
| Phase A inference_pipeline_* untouched | Confirmed |
| No orch wiring (Task 3) | Confirmed — orch not modified |
| Stub selftest still green | PASS |

### Extra smoke (not in committed selftest)
- Empty stdout + `client_rc=0` → `ok=False`
- Each fail marker → `ok=False`, `decode_failed=True`
- Temp dir with executables but no marker → `RpcProbeError`; with marker bytes → probe OK

## Concerns
- No BIN-gated live generate exercised in this task (units only); real `llama-server`/`llama-cli` path waits for Task 3+ with BIN+model.
- `llama-server` HTTP port walks candidates near the RPC port; bind races under heavy parallel use are mitigated by retries but not exhaustively tested without BIN.
- `run_rpc_generate` leaves the RPC server running for the caller to `stop_proc` (by design for orch reuse).

## Not done (out of scope)
- Task 3 orch `--backend` wiring
- Push to remote


## Review fixes (Task 2)

- Added focused selftests for `_port_candidates` exhaustion and unpaired `run_rpc_generate` server arguments before implementation (RED).
- Bounded `_port_candidates` at port 65535 and raise `ValueError` when the requested count cannot be collected.
- `run_rpc_generate` now requires `rpc_server_proc` and `rpc_port` together or neither, preventing an implicit second RPC server.
- Preserved HTTP success gating: only HTTP status >= 500 fails on status; HTTP statuses below 500 still require a non-empty response body.

Validation (GREEN):

```bash
python3 scripts/portable_rpc_selftest.py
python3 scripts/portable_dual_topology_selftest.py
```

Both passed.
