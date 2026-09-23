# Same-Host llama.cpp RPC Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the Phase B portable dual-topology harness with `--backend stub|llamacpp-rpc` so same-host real llama.cpp RPC proves both product topologies (and kill/restart) without breaking stub CI.

**Architecture:** Keep hub/peers/discovery/plan as the control plane. Add `portable_dual_topology_rpc.py` as the data-plane adapter: probe `#26724` BIN, start one loopback `ggml-rpc-server` (worker seat), run one client (frontend seat) with the GGUF, judge success from decode/HTTP status (never process exit alone), and on peer loss tear down + restart the topology once. Default `--backend stub` leaves today's SCPT path untouched.

**Tech Stack:** Python 3 stdlib (existing harness), llama.cpp (`ggml-rpc-server` + `llama-server` preferred / `llama-cli` fallback), env `SHARECOMPUTE_LLAMA_BIN`/`BIN` + model path, existing portable scripts.

**Spec:** `docs/superpowers/specs/2026-09-22-llamacpp-rpc-same-host-design.md` (PR #22)

## Global Constraints

- Seats: exactly `ios` + `windows`; CLI `iphone-frontend` / `windows-frontend` / `both` (default both; exit 0 only if both succeed).
- Service id stays `sharecompute-portable`; do **not** modify Phase A `scripts/inference_pipeline_*`.
- Backend default: `stub`. `--backend llamacpp-rpc` requires usable `#26724` BIN; no silent stub/local fallback.
- RPC endpoints per topology run: **at most one** `ggml-rpc-server` (worker) + one client (≤2 endpoints total).
- MVP tensor placement: client uses `--rpc 127.0.0.1:<worker_port>` and `-ngl 99` so compute lands on the worker RPC device (spike-compatible). Plan ranks still label frontend vs worker seats in logs.
- Status: success only if no `llama_decode == -3` / HTTP 500-equivalent and tokens only after status OK. Never treat `llama-cli` exit 0 alone as success after a kill.
- Peer loss: tear down + **one** automatic topology restart; second failure → exit ≠ 0. No latch reattach.
- Ports: loopback, below host ephemeral range (spike rule), bind-retry; never collide with discovery `37777`.
- Auth/TLS: none (loopback PoC only).
- Stub `--kill-worker` still expects exit ≠ 0 with no restart. RPC `--kill-worker` uses detect-fail + one restart.
- TDD: failing test → implement → pass → commit per task.

## File structure

| Path | Responsibility |
|---|---|
| `scripts/portable_dual_topology_lib.py` | Add `BACKEND_CHOICES`, bin/model env helpers, RPC port-base helper |
| `scripts/portable_dual_topology_rpc.py` | **New:** probe BIN, serve RPC, run client, parse status, stop helpers |
| `scripts/portable_dual_topology_orch.py` | `--backend`, wire adapter, RPC kill targets, restart loop |
| `scripts/portable_dual_topology_peer.py` | When backend=rpc: JOIN/PLAN only; skip SCPT activation |
| `scripts/portable_dual_topology_demo.py` | Docstring examples for `--backend` |
| `scripts/portable_dual_topology_selftest.py` | Stub regression unchanged (update doc needles in Task 7) |
| `scripts/portable_rpc_selftest.py` | **New:** unit tests always; subprocess RPC matrix only if BIN+model present |
| `docs/PORTABLE-DUAL-TOPOLOGY-SIM.md` | Stub vs RPC how-to, BIN/model, kill/restart |

## Review focus

1. Missing BIN with `--backend llamacpp-rpc` must exit ≠ 0 (no silent stub).
2. Success never from `llama-cli` exit 0 alone after a kill.
3. RPC kill targets `ggml-rpc-server`, records `SIGKILL-ok:`, then one restart.
4. `--topology both` still requires both topologies OK.
5. Stub selftest remains green with default backend.

---

### Task 1: Lib helpers — backend choices, BIN/model, RPC ports

**Files:**
- Modify: `scripts/portable_dual_topology_lib.py`
- Create: `scripts/portable_rpc_selftest.py`

**Interfaces:**
- Produces:
  - `BACKEND_CHOICES = ("stub", "llamacpp-rpc")`
  - `DEFAULT_BACKEND = "stub"`
  - `DEFAULT_RPC_MODEL_NAME = "qwen2.5-0.5b-instruct-q4_k_m.gguf"`
  - `def resolve_llama_bin() -> Optional[str]` — `SHARECOMPUTE_LLAMA_BIN` then `BIN`
  - `def resolve_llama_model() -> Optional[str]` — `SHARECOMPUTE_LLAMA_MODEL` then `MODEL`
  - `def rpc_port_base(pid: int) -> int` — spike-compatible below ephemeral range

- [ ] **Step 1: Write failing unit tests**

Create `scripts/portable_rpc_selftest.py`:

```python
#!/usr/bin/env python3
"""Unit + optional BIN-gated tests for portable llamacpp-rpc backend."""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def test_backend_constants() -> None:
    import portable_dual_topology_lib as lib

    assert lib.BACKEND_CHOICES == ("stub", "llamacpp-rpc")
    assert lib.DEFAULT_BACKEND == "stub"
    assert lib.DEFAULT_RPC_MODEL_NAME == "qwen2.5-0.5b-instruct-q4_k_m.gguf"
    print("PASS: backend constants")


def test_resolve_llama_bin_and_model() -> None:
    import portable_dual_topology_lib as lib

    for k in (
        "SHARECOMPUTE_LLAMA_BIN",
        "BIN",
        "SHARECOMPUTE_LLAMA_MODEL",
        "MODEL",
    ):
        os.environ.pop(k, None)
    assert lib.resolve_llama_bin() is None
    assert lib.resolve_llama_model() is None
    os.environ["BIN"] = "/tmp/fake-bin"
    os.environ["MODEL"] = "/tmp/fake.gguf"
    assert lib.resolve_llama_bin() == "/tmp/fake-bin"
    assert lib.resolve_llama_model() == "/tmp/fake.gguf"
    os.environ["SHARECOMPUTE_LLAMA_BIN"] = "/tmp/sc-bin"
    os.environ["SHARECOMPUTE_LLAMA_MODEL"] = "/tmp/sc.gguf"
    assert lib.resolve_llama_bin() == "/tmp/sc-bin"
    assert lib.resolve_llama_model() == "/tmp/sc.gguf"
    print("PASS: resolve bin/model")


def test_rpc_port_base_below_ephemeral() -> None:
    import portable_dual_topology_lib as lib

    base = lib.rpc_port_base(12345)
    assert 10000 <= base <= 30000
    assert base + 64 < 32768
    print("PASS: rpc_port_base")


def run_unit_tests() -> None:
    test_backend_constants()
    test_resolve_llama_bin_and_model()
    test_rpc_port_base_below_ephemeral()


def main() -> int:
    run_unit_tests()
    print("ALL PASS: portable_rpc_selftest (units)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python3 scripts/portable_rpc_selftest.py
```

Expected: FAIL (missing attributes / import errors).

- [ ] **Step 3: Write minimal lib implementation**

Append to `scripts/portable_dual_topology_lib.py` (ensure `os` and `Optional` already imported):

```python
BACKEND_CHOICES = ("stub", "llamacpp-rpc")
DEFAULT_BACKEND = "stub"
DEFAULT_RPC_MODEL_NAME = "qwen2.5-0.5b-instruct-q4_k_m.gguf"


def resolve_llama_bin() -> Optional[str]:
    for key in ("SHARECOMPUTE_LLAMA_BIN", "BIN"):
        val = os.environ.get(key)
        if val:
            return val
    return None


def resolve_llama_model() -> Optional[str]:
    for key in ("SHARECOMPUTE_LLAMA_MODEL", "MODEL"):
        val = os.environ.get(key)
        if val:
            return val
    return None


def rpc_port_base(pid: int) -> int:
    """Spike-compatible: ports below host ephemeral range."""
    ephemeral_lo = 32768
    try:
        with open("/proc/sys/net/ipv4/ip_local_port_range", encoding="utf-8") as fh:
            ephemeral_lo = int(fh.read().split()[0])
    except OSError:
        pass
    base = 20000 + ((pid * 7) % 10000)
    if base + 64 >= ephemeral_lo:
        base = max(10000, ephemeral_lo - 5000)
    return base
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 scripts/portable_rpc_selftest.py
python3 scripts/portable_dual_topology_selftest.py
```

Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/portable_dual_topology_lib.py scripts/portable_rpc_selftest.py
git commit -m "feat(portable-rpc): lib backend/BIN/model/port helpers"
```

---

### Task 2: RPC adapter — probe, serve, client, status

**Files:**
- Create: `scripts/portable_dual_topology_rpc.py`
- Modify: `scripts/portable_rpc_selftest.py`

**Interfaces:**
- Consumes: `resolve_llama_bin`, `resolve_llama_model`, `rpc_port_base`, `DEFAULT_RPC_MODEL_NAME`
- Produces:
  - `class RpcProbeError(Exception)`
  - `@dataclass class RpcRunResult`: `ok: bool`, `detail: str`, `client_rc: Optional[int]`, `decode_failed: bool`, `http_status: Optional[int]`
  - `def probe_llama_bin(bin_dir: str) -> None`
  - `def start_rpc_server(bin_dir: str, host: str = "127.0.0.1") -> tuple[Popen, int]`
  - `def stop_proc(proc: Optional[Popen]) -> None`
  - `def parse_cli_logs(stderr_text: str, stdout_text: str, client_rc: Optional[int]) -> RpcRunResult`
  - `def run_rpc_generate(*, bin_dir=None, model=None, prompt=..., n_tokens=32, timeout_s=180.0, rpc_server_proc=None, rpc_port=None) -> tuple[RpcRunResult, Popen, int]`
  - Prefer `llama-server` + `/completion` if present; else `llama-cli` with log parsing

- [ ] **Step 1: Write failing tests**

Add to `portable_rpc_selftest.py` and call from `run_unit_tests()`:

```python
def test_probe_missing_bin_raises() -> None:
    from portable_dual_topology_rpc import RpcProbeError, probe_llama_bin

    try:
        probe_llama_bin("/tmp/sharecompute-missing-llama-bin")
        _fail("probe must raise for missing bin")
    except RpcProbeError:
        pass
    print("PASS: probe missing bin")


def test_parse_cli_status_detects_decode_fail() -> None:
    from portable_dual_topology_rpc import parse_cli_logs

    bad = parse_cli_logs(
        "llama_decode: failed to decode, ret = -3\n",
        "Explain gravity\nGravity is...\n",
        client_rc=0,
    )
    assert bad.decode_failed is True and bad.ok is False
    good = parse_cli_logs(
        "assigned to device RPC0\n",
        "Explain gravity\nGravity is a force.\n",
        client_rc=0,
    )
    assert good.decode_failed is False and good.ok is True
    print("PASS: parse_cli_logs")
```

- [ ] **Step 2: Run — expect FAIL**

```bash
python3 scripts/portable_rpc_selftest.py
```

- [ ] **Step 3: Implement `scripts/portable_dual_topology_rpc.py`**

Implement the module with these non-negotiable behaviors:

1. `probe_llama_bin`: require executable `ggml-rpc-server` plus `llama-server` and/or `llama-cli`; require binary/lib bytes contain marker `the device is now unusable` (#26724); else raise `RpcProbeError`.
2. `start_rpc_server`: bind `127.0.0.1` with ports from `rpc_port_base(os.getpid())`, retry up to 5 ports, prove listen via `socket.create_connection`, set `LD_LIBRARY_PATH` to `bin_dir`.
3. `parse_cli_logs`: `ok=False` if any of `llama_decode: failed to decode, ret = -3`, `ret = -3`, `Compute error`, `graph computation failed`, `crashed or returned a malformed response`; also `ok=False` if stdout empty; **do not** treat `client_rc==0` as sufficient alone.
4. `run_rpc_generate`: probe; resolve model file (raise if missing); start server if not passed; if `llama-server` exists, POST `http://127.0.0.1:<http_port>/completion` with JSON `prompt`/`n_predict`/`stream=false` and fail on HTTP ≥500; else run `llama-cli` with `-m MODEL --rpc 127.0.0.1:PORT -ngl 99 -c 8192 -st -no-cnv -v -n N -p PROMPT` and stdin closed; return `(result, server_proc, port)`.
5. `stop_proc`: SIGTERM then kill.

Keep the implementation in one file; mirror spike `CLI_COMMON` and port rules from `Spikes/llamacpp-rpc/run.sh`.

- [ ] **Step 4: Run — expect PASS**

```bash
python3 scripts/portable_rpc_selftest.py
```

- [ ] **Step 5: Commit**

```bash
git add scripts/portable_dual_topology_rpc.py scripts/portable_rpc_selftest.py
git commit -m "feat(portable-rpc): adapter probe/serve/generate/status"
```

---

### Task 3: CLI `--backend` + missing-BIN loud fail

**Files:**
- Modify: `scripts/portable_dual_topology_orch.py`
- Modify: `scripts/portable_dual_topology_demo.py`
- Modify: `scripts/portable_rpc_selftest.py`

**Interfaces:**
- Orch accepts `--backend` (`BACKEND_CHOICES`, default `DEFAULT_BACKEND`)
- Orchestrator role with `llamacpp-rpc` probes BIN before spawn; missing/bad BIN → exit 1 with clear stderr

- [ ] **Step 1: Failing subprocess test**

```python
def test_backend_rpc_missing_bin_exits_nonzero() -> None:
    demo = os.path.join(HERE, "portable_dual_topology_demo.py")
    env = os.environ.copy()
    env.pop("SHARECOMPUTE_LLAMA_BIN", None)
    env.pop("BIN", None)
    cp = subprocess.run(
        [
            sys.executable,
            demo,
            "--backend",
            "llamacpp-rpc",
            "--topology",
            "iphone-frontend",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        cwd=os.path.dirname(HERE),
    )
    if cp.returncode == 0:
        _fail("llamacpp-rpc without BIN must exit != 0")
    out = cp.stdout + cp.stderr
    if "BIN" not in out and "llama" not in out.lower():
        _fail("missing clear BIN error message")
    print("PASS: missing BIN loud fail")
```

Call from `main()` always.

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Wire CLI**

In `build_parser()` add:

```python
p.add_argument(
    "--backend",
    choices=BACKEND_CHOICES,
    default=DEFAULT_BACKEND,
    help="Data plane: stub (SCPT, default) or llamacpp-rpc (requires #26724 BIN)",
)
```

Import `BACKEND_CHOICES`, `DEFAULT_BACKEND` from lib.

In `main()`, when `args.role == "orchestrator"` and `args.backend == "llamacpp-rpc"`:

```python
from portable_dual_topology_rpc import RpcProbeError, probe_llama_bin
from portable_dual_topology_lib import resolve_llama_bin

bin_dir = resolve_llama_bin()
if not bin_dir:
    print(
        "error: --backend llamacpp-rpc requires SHARECOMPUTE_LLAMA_BIN or BIN",
        file=sys.stderr,
    )
    return 1
try:
    probe_llama_bin(bin_dir)
except RpcProbeError as exc:
    print(f"error: {exc}", file=sys.stderr)
    return 1
```

Thread `backend` into `run_orchestrator` / `run_one_topology` signatures (stub path unchanged except logging `backend=stub`).

Update `portable_dual_topology_demo.py` module docstring with `--backend` examples.

- [ ] **Step 4: Run**

```bash
python3 scripts/portable_rpc_selftest.py
python3 scripts/portable_dual_topology_selftest.py
```

- [ ] **Step 5: Commit**

```bash
git add scripts/portable_dual_topology_orch.py scripts/portable_dual_topology_demo.py scripts/portable_rpc_selftest.py
git commit -m "feat(portable-rpc): --backend flag and missing-BIN fail"
```

---

### Task 4: Integrate RPC generate after control-plane plan (happy path)

**Files:**
- Modify: `scripts/portable_dual_topology_orch.py`
- Modify: `scripts/portable_dual_topology_peer.py`
- Modify: `scripts/portable_rpc_selftest.py`

**Behavior when `backend=="llamacpp-rpc"`:**

1. Spawn hub + peers as today until both seats JOIN and plan is accepted (`accepted plan` / `Broadcasting shard plan` in progress events).
2. Set peer env `SHARECOMPUTE_PORTABLE_BACKEND=llamacpp-rpc`. Peers **skip the SCPT activation loop** after accepting plan and wait until process teardown (control-plane membership only).
3. Orch calls `run_rpc_generate(n_tokens=min(token_count, 32), timeout_s=timeout_s)`.
4. Log `backend=llamacpp-rpc`, `frontend=<seat>`, `worker=<seat>`, `rpc_endpoint=127.0.0.1:PORT`.
5. If `result.ok`: `stop_proc` server, `terminate_all`, return 0. Else return 1.
6. If plan never accepted: return 1 without starting llama.
7. When `backend=="stub"`: zero behavior change from Phase B.

**YAGNI note:** RPC mode judges data plane via adapter after successful join+plan; hub `done` from SCPT is not required for RPC exit 0.

Peer change (after plan accept):

```python
if os.environ.get("SHARECOMPUTE_PORTABLE_BACKEND") == "llamacpp-rpc":
    # Wait until orch tears us down; do not run SCPT.
    while time.monotonic() < deadline:
        msg = recv_line(...)  # optional hub messages
        if msg and msg.get("type") in ("error", "shutdown"):
            return 1 if msg.get("type") == "error" else 0
        time.sleep(0.05)
    return 0
```

- [ ] **Step 1: BIN-gated happy test**

```python
def _have_rpc_env() -> bool:
    from portable_dual_topology_lib import resolve_llama_bin, resolve_llama_model
    from portable_dual_topology_rpc import RpcProbeError, probe_llama_bin

    b, m = resolve_llama_bin(), resolve_llama_model()
    if not b or not m or not os.path.isfile(m):
        return False
    try:
        probe_llama_bin(b)
        return True
    except RpcProbeError:
        return False


def test_rpc_happy_iphone_frontend() -> None:
    if not _have_rpc_env():
        print("SKIP: rpc happy (no BIN/model)")
        return
    demo = os.path.join(HERE, "portable_dual_topology_demo.py")
    cp = subprocess.run(
        [
            sys.executable,
            demo,
            "--backend",
            "llamacpp-rpc",
            "--topology",
            "iphone-frontend",
            "--timeout",
            "120",
            "--token-count",
            "16",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=os.path.dirname(HERE),
    )
    if cp.returncode != 0:
        sys.stderr.write(cp.stdout + "\n" + cp.stderr)
        _fail(f"rpc iphone-frontend expected 0, got {cp.returncode}")
    print("PASS: rpc happy iphone-frontend")
```

- [ ] **Step 2: Run test — FAIL until wired**

- [ ] **Step 3: Implement orch/peer integration as specified**

- [ ] **Step 4: Run**

```bash
python3 scripts/portable_dual_topology_selftest.py
python3 scripts/portable_rpc_selftest.py
# with BIN+MODEL set, happy case should PASS
```

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(portable-rpc): orch runs RPC generate after plan"
```

---

### Task 5: Kill `ggml-rpc-server` + one topology restart

**Files:**
- Modify: `scripts/portable_dual_topology_orch.py`
- Modify: `scripts/portable_rpc_selftest.py`

**RPC kill policy (spec):**

- `--kill-worker mid|start` SIGKILLs the **`ggml-rpc-server`** PID (worker compute).
- Record `SIGKILL-ok:rpc-server:pid=...:exit=...`.
- Attempt 1 after kill must **not** report success (`result.ok` must be False / decode fail / HTTP 500).
- Tear down everything; restart full topology once (fresh ports/processes).
- If attempt 2 `result.ok`: topology exit 0.
- If attempt 2 fails: exit ≠ 0.
- Stub `--kill-worker` path unchanged (exit ≠ 0, no restart).

Sketch inside `run_one_topology` for `backend=="llamacpp-rpc"`:

```python
max_attempts = 2 if kill_worker else 1
for attempt in range(1, max_attempts + 1):
    # spawn hub/peers; wait for plan
    srv_proc, port = start_rpc_server(bin_dir)
    kill_evidence = []
    if kill_worker:
        # start generate in thread OR start client then kill server mid-flight
        # mid: wait until client log shows generation / RPC traffic, then kill srv_proc
        # start: kill shortly after server listen, before/at client start
        ...
        kill_evidence.append(
            f"SIGKILL-ok:rpc-server:pid={srv_proc.pid}:exit={srv_proc.returncode}"
        )
    result, srv_proc, port = run_rpc_generate(..., rpc_server_proc=srv_proc, rpc_port=port)
    if kill_worker and attempt == 1:
        if result.ok:
            print("FAIL: kill attempt silently succeeded", flush=True)
            stop_proc(srv_proc); terminate_all(); return 1
        if not any(e.startswith("SIGKILL-ok:") for e in kill_evidence):
            print("FAIL: missing SIGKILL-ok evidence", flush=True)
            stop_proc(srv_proc); terminate_all(); return 1
        print("rpc attempt 1 failed after kill; restarting topology", flush=True)
        stop_proc(srv_proc); terminate_all()
        continue
    stop_proc(srv_proc); terminate_all()
    return 0 if result.ok else 1
```

- [ ] **Step 1: BIN-gated kill tests** asserting `SIGKILL-ok:` and either restart success log or loud final fail — never silent success on attempt 1.

- [ ] **Step 2–4: Implement; keep stub selftest green; commit**

```bash
git commit -am "feat(portable-rpc): kill rpc-server + one topology restart"
```

---

### Task 6: RPC selftest matrix

**Files:**
- Modify: `scripts/portable_rpc_selftest.py`

**Always:** unit tests + missing-BIN test.

**If `_have_rpc_env()`:** happy `iphone-frontend`, `windows-frontend`, `both`; kill mid; kill start.

```python
def main() -> int:
    run_unit_tests()
    test_backend_rpc_missing_bin_exits_nonzero()
    if _have_rpc_env():
        test_rpc_happy_iphone_frontend()
        test_rpc_happy_windows_frontend()
        test_rpc_happy_both()
        test_rpc_kill_mid_restarts_once()
        test_rpc_kill_start_restarts_once()
    else:
        print("SKIP: BIN-gated rpc matrix (set SHARECOMPUTE_LLAMA_BIN + MODEL)")
    print("ALL PASS: portable_rpc_selftest")
    return 0
```

- [ ] **Run both selftests; commit**

```bash
python3 scripts/portable_dual_topology_selftest.py
python3 scripts/portable_rpc_selftest.py
git commit -am "test(portable-rpc): BIN-gated matrix + always-on units"
```

---

### Task 7: Docs — stub vs RPC runbook

**Files:**
- Modify: `docs/PORTABLE-DUAL-TOPOLOGY-SIM.md`
- Modify: `scripts/portable_dual_topology_selftest.py` doc needles

Document:

- `--backend stub` (default) vs `--backend llamacpp-rpc`
- Env vars `SHARECOMPUTE_LLAMA_BIN`/`BIN`, `SHARECOMPUTE_LLAMA_MODEL`/`MODEL`
- `#26724` requirement and probe marker `the device is now unusable`
- Same-host only; no auth/TLS; one worker RPC endpoint
- Kill/restart policy for RPC vs stub
- Commands:

```bash
python3 scripts/portable_dual_topology_demo.py --backend stub
SHARECOMPUTE_LLAMA_BIN=/path/to/bin \
SHARECOMPUTE_LLAMA_MODEL=/path/to/qwen2.5-0.5b-instruct-q4_k_m.gguf \
  python3 scripts/portable_dual_topology_demo.py --backend llamacpp-rpc --topology both
python3 scripts/portable_rpc_selftest.py
```

Update “Next” to: LAN Windows↔iPhone after this gate.

Extend `test_docs_exist_and_mention_commands` needles with `--backend`, `llamacpp-rpc`, `SHARECOMPUTE_LLAMA_BIN`.

- [ ] **Commit:** `docs: portable RPC same-host gate runbook`

---

### Task 8: Full verification gate

- [ ] **Stub regression**

```bash
python3 scripts/portable_dual_topology_selftest.py
```

Expected: ALL PASS.

- [ ] **RPC units + missing BIN**

```bash
env -u BIN -u SHARECOMPUTE_LLAMA_BIN -u MODEL -u SHARECOMPUTE_LLAMA_MODEL \
  python3 scripts/portable_rpc_selftest.py
```

Expected: units PASS; missing-BIN PASS; matrix SKIP.

- [ ] **RPC full matrix (when BIN available)**

```bash
export SHARECOMPUTE_LLAMA_BIN=/path/to/26724/bin
export SHARECOMPUTE_LLAMA_MODEL=/path/to/qwen2.5-0.5b-instruct-q4_k_m.gguf
python3 scripts/portable_rpc_selftest.py
python3 scripts/portable_dual_topology_demo.py --backend llamacpp-rpc --topology both
```

- [ ] **Phase A untouched**

```bash
git diff --name-only origin/main...HEAD | grep -E 'inference_pipeline_|four_platform_' && exit 1 || true
```

- [ ] **Open implementation PR** with gate results; do not claim LAN.

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| Approach 2 adapter `stub\|llamacpp-rpc` | 1–4 |
| Keep seats / topology / both aggregate | 4, 6 |
| `#26724` BIN probe; missing BIN loud fail | 2, 3 |
| Model default Qwen 0.5B | 1, 2 |
| Status ≠ process exit; no `/health`-alone | 2 |
| One worker RPC endpoint; same-host ports | 2, 4 |
| Stub CI default | 3, 6, 8 |
| Kill → detect + one restart | 5 |
| Docs | 7 |
| Phase A untouched | 8 |
| Non-goals (LAN, Swift, auth, 3+ peers) | documented; no tasks |

## Placeholder / consistency review

- Layer CLI pinned: `--rpc 127.0.0.1:PORT` + `-ngl 99` (one worker).
- Names: `probe_llama_bin`, `run_rpc_generate`, `RpcRunResult`, `SHARECOMPUTE_PORTABLE_BACKEND`.
- Stub kill ≠ RPC kill policy — explicit in Task 5 and Global Constraints.

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-22-llamacpp-rpc-same-host.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks
2. **Inline Execution** — execute tasks in this session with checkpoints

Which approach?
