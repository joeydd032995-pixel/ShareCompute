# Phase B Portable Dual-Topology Stub Sim Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a multi-process portable-protocol stub that proves both product topologies (iPhone frontend + Windows worker, and Windows frontend + iPhone worker) with loud hard fails, default `--topology both`, and docs — without touching Phase A scripts.

**Architecture:** Fork Phase A hub/peer/orch/demo shape into new `scripts/portable_dual_topology_*.py` files with distinct discovery service id `sharecompute-portable` and TCP magic `SCPT`. Exactly two seats (`ios`, `windows`); orch maps CLI topology names to platform+role pairs, runs one topology (or both back-to-back), and aggregates exit codes. Fake-but-sized compute only — no llama.cpp.

**Tech Stack:** Python 3.10+ stdlib only (`argparse`, `socket`, `subprocess`, `threading`, `struct`, `hashlib`, `json`, `select`, `signal`, `uuid`, `time`); reuse UDP multicast + TCP line framing helpers from `scripts/four_platform_pool_lib.py`; no pytest dependency — gate via `scripts/portable_dual_topology_selftest.py` subprocess exit-code matrix.

**Spec:** `docs/superpowers/specs/2026-09-22-portable-dual-topology-design.md`

## Global Constraints

- Product seats only: `ios` and `windows` (Mac/Android out of this harness).
- Service id: `sharecompute-portable` (must not equal `sharecompute-infer` or `sharecompute-pool`).
- Data-plane TCP magic: `SCPT` (must not equal Phase A `SCIN`).
- CLI topology values: `iphone-frontend`, `windows-frontend`, `both` (default `both`).
- Wire/platform enum stays `ios` (not `iphone`); product logs may say iPhone.
- Topology map: `iphone-frontend` → frontend=`ios`, worker=`windows`; `windows-frontend` → frontend=`windows`, worker=`ios`.
- Overall exit 0 for `--topology both` only if **both** topology runs exit 0.
- Do **not** modify `scripts/inference_pipeline_*` or `docs/INFERENCE-PIPELINE-SIM.md`.
- Fake compute only (checksum + sleep × layers); no ggml-rpc / llama.cpp / MLX.
- Loud fails: exit ≠ 0, no hang; kill cases require SIGKILL evidence in logs.
- Model sizing must fit default ios(6GB)+windows(16GB) seats after overhead (use ~12GB demo model, not Phase A 24GB four-seat model).

## File Structure

| Path | Responsibility |
|---|---|
| `scripts/portable_dual_topology_lib.py` | Constants, seats, topology map, SCPT framing, portable plan helpers, fake compute |
| `scripts/portable_dual_topology_hub.py` | Control-plane hub: beacon, JOIN role/platform enforcement, plan broadcast |
| `scripts/portable_dual_topology_peer.py` | Frontend/worker peer: discover, JOIN, plan accept, activation pipeline |
| `scripts/portable_dual_topology_orch.py` | CLI + spawn hub/peers per topology; both aggregation; fail knobs |
| `scripts/portable_dual_topology_demo.py` | Thin entry that calls orch `main()` |
| `scripts/portable_dual_topology_selftest.py` | Unit checks + subprocess exit-code matrix (PASS/FAIL gate) |
| `docs/PORTABLE-DUAL-TOPOLOGY-SIM.md` | Success + fail commands, architecture, Next→RPC handoff |

## Review Focus

1. **Wrong service id on beacon** — peer must ignore `sharecompute-infer` / `sharecompute-pool` beacons and fail discovery for portable service only.
2. **Wrong data magic `SCIN`** — receiver must raise / pipeline-error on non-`SCPT` frames (no silent accept).
3. **`--topology both` partial success** — if topology A exits 0 and B exits 1, overall must be 1 (never mask with last-wins=0).
4. **Role mismatch on both topologies** — hub reject must work when frontend seat is ios *and* when it is windows.
5. **Kill evidence without hang** — SIGKILL mid must record `SIGKILL-ok:` evidence and return ≠0 within overall timeout.

---

### Task 1: Lib constants, topology map, SCPT framing, portable plan

**Files:**
- Create: `scripts/portable_dual_topology_lib.py`
- Create: `scripts/portable_dual_topology_selftest.py`
- Test: `scripts/portable_dual_topology_selftest.py`

**Interfaces:**
- Consumes: `four_platform_pool_lib` (`Peer`, `plan_shards`, `PROTOCOL_VERSION`, `DEFAULT_HOST`, `DEFAULT_TIMEOUT_S`, `DISCOVERY_MULTICAST_GROUP`, `DISCOVERY_PORT`, `BEACON_INTERVAL_S`, `HUB_READY_PREFIX`, `HubBeacon`, `open_beacon_listener`, `open_beacon_sender`, `encode_msg`, `recv_line`, `send_msg`)
- Produces:
  - `PORTABLE_SERVICE_ID: str = "sharecompute-portable"`
  - `PORTABLE_SEATS: tuple[str, ...] = ("ios", "windows")`
  - `PORTABLE_DISPLAY: dict[str, str]` with `ios→"iPhone"`, `windows→"Windows"`
  - `PORTABLE_DEFAULT_USABLE_GB: dict[str, float]` with `ios→6.0`, `windows→16.0`
  - `ROLES: tuple[str, ...] = ("frontend", "worker")`
  - `TOPOLOGY_CHOICES: tuple[str, ...] = ("iphone-frontend", "windows-frontend", "both")`
  - `resolve_topology(name: str) -> tuple[str, str]` → `(frontend_platform, worker_platform)`; raises `ValueError` for `"both"` or unknown
  - `DATA_HEADER_MAGIC: bytes = b"SCPT"`
  - Dataclasses: `PortablePeer`, `PortableShard`, `ActivationMsg`
  - `encode_portable_beacon(...) -> bytes`, `parse_portable_beacon(raw: bytes) -> Optional[HubBeacon]`
  - `announce_portable_beacon(...)`, `discover_portable_hub(...) -> Optional[HubBeacon]`
  - `plan_portable_shards(peers, layer_count=32, total_weight_gb=12.0, overhead_gb=0.5) -> list[PortableShard]`
  - `shards_to_plan_dict(...)`, `plan_dict_to_shards(...)`
  - `encode_activation`, `recv_activation`, `send_activation`
  - `fake_compute`, `make_prompt_activation`, `buffer_budget_bytes`, `activation_nbytes_for_budget`
  - `open_data_listener`, `connect_with_deadline`
  - Constants: `DEFAULT_LAYER_COUNT=32`, `DEFAULT_TOTAL_WEIGHT_GB=12.0`, `DEFAULT_OVERHEAD_GB=0.5`, `DEFAULT_TOKEN_COUNT=8`, `DEFAULT_ACTIVATION_BYTES=4096`

- [ ] **Step 1: Write the failing selftest for lib unit checks**

Create `scripts/portable_dual_topology_selftest.py` with:

```python
#!/usr/bin/env python3
"""Selftest for Phase B portable dual-topology stub (unit + subprocess matrix)."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

DEMO = os.path.join(HERE, "portable_dual_topology_demo.py")


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def test_lib_constants_and_topology() -> None:
    import portable_dual_topology_lib as lib

    assert lib.PORTABLE_SERVICE_ID == "sharecompute-portable"
    assert lib.PORTABLE_SERVICE_ID not in ("sharecompute-infer", "sharecompute-pool")
    assert lib.DATA_HEADER_MAGIC == b"SCPT"
    assert lib.DATA_HEADER_MAGIC != b"SCIN"
    assert lib.PORTABLE_SEATS == ("ios", "windows")
    assert "macos" not in lib.PORTABLE_SEATS and "android" not in lib.PORTABLE_SEATS
    assert lib.resolve_topology("iphone-frontend") == ("ios", "windows")
    assert lib.resolve_topology("windows-frontend") == ("windows", "ios")
    try:
        lib.resolve_topology("both")
        _fail("resolve_topology('both') must raise")
    except ValueError:
        pass
    try:
        lib.resolve_topology("macos-frontend")
        _fail("unknown topology must raise")
    except ValueError:
        pass


def test_scpt_roundtrip_rejects_scin() -> None:
    import portable_dual_topology_lib as lib

    msg = lib.ActivationMsg(epoch="abc", token_id=1, rank_from=0, rank_to=1, payload=b"x" * 64)
    framed = lib.encode_activation(msg)
    assert framed[:4] == b"SCPT"
    bad = b"SCIN" + framed[4:]
    a, b = socket.socketpair()
    try:
        a.setblocking(False)
        b.setblocking(False)
        b.sendall(bad)
        try:
            lib.recv_activation(a, time.monotonic() + 1.0)
            _fail("SCIN magic must be rejected")
        except ValueError:
            pass
    finally:
        a.close()
        b.close()


def test_plan_fits_two_seats_rejects_tiny_ram() -> None:
    import portable_dual_topology_lib as lib

    peers = [
        lib.PortablePeer("ios", "sim-ios", 6.0, "frontend", "127.0.0.1", 9001),
        lib.PortablePeer("windows", "sim-windows", 16.0, "worker", "127.0.0.1", 9002),
    ]
    shards = lib.plan_portable_shards(peers)
    assert len(shards) == 2
    assert shards[0].role == "frontend" and shards[0].platform == "ios" and shards[0].rank == 0
    tiny = [
        lib.PortablePeer("ios", "sim-ios", 0.1, "frontend", "127.0.0.1", 9001),
        lib.PortablePeer("windows", "sim-windows", 0.1, "worker", "127.0.0.1", 9002),
    ]
    try:
        lib.plan_portable_shards(tiny)
        _fail("tiny RAM must reject plan")
    except ValueError:
        pass


def test_beacon_service_filter() -> None:
    import json
    import portable_dual_topology_lib as lib

    good = json.dumps(
        {
            "v": 1,
            "service": "sharecompute-portable",
            "hub_host": "127.0.0.1",
            "hub_port": 5555,
            "epoch": "deadbeef",
            "platforms": ["ios", "windows"],
        }
    ).encode()
    bad = json.dumps(
        {
            "v": 1,
            "service": "sharecompute-infer",
            "hub_host": "127.0.0.1",
            "hub_port": 5555,
            "epoch": "deadbeef",
            "platforms": ["ios", "windows"],
        }
    ).encode()
    assert lib.parse_portable_beacon(good) is not None
    assert lib.parse_portable_beacon(bad) is None


def run_unit_tests() -> None:
    test_lib_constants_and_topology()
    test_scpt_roundtrip_rejects_scin()
    test_plan_fits_two_seats_rejects_tiny_ram()
    test_beacon_service_filter()
    print("PASS: lib unit checks")


def main() -> int:
    run_unit_tests()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 scripts/portable_dual_topology_selftest.py`

Expected: FAIL with `ModuleNotFoundError: No module named 'portable_dual_topology_lib'`

- [ ] **Step 3: Write minimal implementation**

Create `scripts/portable_dual_topology_lib.py` by forking `scripts/inference_pipeline_lib.py` with these exact substitutions and additions:

1. Module docstring mentions Phase B / `sharecompute-portable` / `SCPT`.
2. Replace `INFER_SERVICE_ID` with `PORTABLE_SERVICE_ID = "sharecompute-portable"`.
3. Add `PORTABLE_SEATS = ("ios", "windows")`, `PORTABLE_DISPLAY`, `PORTABLE_DEFAULT_USABLE_GB`, `TOPOLOGY_CHOICES`, `_TOPOLOGY_MAP`, `resolve_topology`.
4. Rename types: `InferPeer`→`PortablePeer`, `InferShard`→`PortableShard`; rename functions `plan_infer_shards`→`plan_portable_shards`, `encode_infer_beacon`→`encode_portable_beacon`, `parse_infer_beacon`→`parse_portable_beacon` (reject unless service==`PORTABLE_SERVICE_ID`), `announce_infer_beacon`→`announce_portable_beacon`, `discover_infer_hub`→`discover_portable_hub`.
5. Set `DATA_HEADER_MAGIC = b"SCPT"` (keep same `DATA_HEADER_FMT` / size).
6. Set `DEFAULT_LAYER_COUNT = 32`, `DEFAULT_TOTAL_WEIGHT_GB = 12.0` (must fit ios 6 + windows 16 after 0.5 overhead/peer).
7. In `plan_portable_shards`: reject any peer whose `platform not in PORTABLE_SEATS`; require exactly one frontend and exactly one worker; order `[frontend, worker]` (no four-seat sort).
8. Keep `ActivationMsg`, `encode_activation`, `recv_activation` (magic check against `SCPT`), `send_activation`, `fake_compute`, `make_prompt_activation`, `buffer_budget_bytes`, `activation_nbytes_for_budget`, `open_data_listener`, `connect_with_deadline`, `shards_to_plan_dict`, `plan_dict_to_shards` with Portable types.
9. Re-export from `four_platform_pool_lib` the same transport helpers Phase A re-exports (`BEACON_INTERVAL_S`, `DEFAULT_HOST`, `DEFAULT_TIMEOUT_S`, `DISCOVERY_*`, `HUB_READY_PREFIX`, `PROTOCOL_VERSION`, `HubBeacon`, `Peer`, `open_beacon_*`, `plan_shards`, `recv_line`, `send_msg`, `encode_msg`).

`resolve_topology` body:

```python
_TOPOLOGY_MAP = {
    "iphone-frontend": ("ios", "windows"),
    "windows-frontend": ("windows", "ios"),
}

def resolve_topology(name: str) -> Tuple[str, str]:
    if name not in _TOPOLOGY_MAP:
        raise ValueError(f"unknown or non-atomic topology: {name!r}")
    return _TOPOLOGY_MAP[name]
```

Do not leave any `INFER_*` / `SCIN` / `macos` seat defaults in this file.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 scripts/portable_dual_topology_selftest.py`

Expected: PASS — prints `PASS: lib unit checks`, exit 0

- [ ] **Step 5: Commit**

```bash
git add scripts/portable_dual_topology_lib.py scripts/portable_dual_topology_selftest.py
git commit -m "$(cat <<'EOF'
feat(portable): add Phase B lib constants, SCPT framing, topology map

Introduce sharecompute-portable service id, ios+windows seats, SCPT
activation framing, and plan helpers sized for two-seat RAM.
EOF
)"
```

---

### Task 2: Hub role enforcement (ios/windows only)

**Files:**
- Create: `scripts/portable_dual_topology_hub.py`
- Modify: `scripts/portable_dual_topology_selftest.py`
- Test: `scripts/portable_dual_topology_selftest.py`

**Interfaces:**
- Consumes: Task 1 lib (`PORTABLE_SEATS`, `PORTABLE_DISPLAY`, `PORTABLE_SERVICE_ID`, `PORTABLE_DEFAULT_USABLE_GB`, `PortablePeer`, `plan_portable_shards`, `shards_to_plan_dict`, `announce_portable_beacon`, `open_beacon_sender`, `recv_line`, `send_msg`, `HUB_READY_PREFIX`, `BEACON_INTERVAL_S`, `DEFAULT_HOST`, `DEFAULT_TOKEN_COUNT`, `DISCOVERY_*`)
- Produces: `run_hub(host: str, port: int, expected: Sequence[str], timeout_s: float, *, frontend_platform: str, token_count: int = DEFAULT_TOKEN_COUNT, enable_beacon: bool = True, discovery_port: int = DISCOVERY_PORT) -> int`

- [ ] **Step 1: Write the failing hub reject checks**

Append to `scripts/portable_dual_topology_selftest.py`:

```python
def test_hub_rejects_non_product_and_role_mismatch() -> None:
    import socket
    import threading
    from portable_dual_topology_hub import run_hub
    from portable_dual_topology_lib import (
        DEFAULT_HOST,
        PORTABLE_DEFAULT_USABLE_GB,
        recv_line,
        send_msg,
    )

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind((DEFAULT_HOST, 0))
    port = probe.getsockname()[1]
    probe.close()

    result = {"rc": None}

    def hub_thread() -> None:
        result["rc"] = run_hub(
            DEFAULT_HOST,
            port,
            ("ios", "windows"),
            timeout_s=3.0,
            frontend_platform="ios",
            token_count=2,
            enable_beacon=False,
        )

    t = threading.Thread(target=hub_thread, daemon=True)
    t.start()
    time.sleep(0.2)

    def join_once(platform: str, role: str, data_port: int) -> dict:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((DEFAULT_HOST, port))
        s.setblocking(False)
        buf = bytearray()
        send_msg(
            s,
            {
                "type": "join",
                "platform": platform,
                "node_id": f"sim-{platform}",
                "usable_gb": PORTABLE_DEFAULT_USABLE_GB.get(platform, 8.0),
                "role": role,
                "data_host": DEFAULT_HOST,
                "data_port": data_port,
            },
        )
        msg = recv_line(s, buf, time.monotonic() + 2.0)
        try:
            s.close()
        except OSError:
            pass
        return msg or {}

    bad = join_once("macos", "worker", 19001)
    assert bad.get("type") == "error", f"expected error for macos, got {bad}"
    assert bad.get("reason") == "non-product-platform"

    mm = join_once("windows", "frontend", 19002)
    assert mm.get("type") == "error", f"expected role reject, got {mm}"
    assert mm.get("reason") == "frontend-role-reserved"

    mm2 = join_once("ios", "worker", 19003)
    assert mm2.get("type") == "error"
    assert mm2.get("reason") == "frontend-must-join-as-frontend"

    t.join(timeout=5.0)
    print("PASS: hub reject checks")
```

Call `test_hub_rejects_non_product_and_role_mismatch()` from `run_unit_tests()`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 scripts/portable_dual_topology_selftest.py`

Expected: FAIL with `ModuleNotFoundError: No module named 'portable_dual_topology_hub'`

- [ ] **Step 3: Write minimal implementation**

Create `scripts/portable_dual_topology_hub.py` by forking `scripts/inference_pipeline_hub.py` with these exact differences:

1. Import only from `portable_dual_topology_lib` (and stdlib). Banner: `ShareCompute portable dual-topology sim (Phase B) — control-plane hub`.
2. On JOIN: if `platform not in PORTABLE_SEATS`, send `{"type":"error","reason":"non-product-platform"}` and return (do not accept).
3. Keep Phase A frontend reservation:
   - `role == "frontend" and platform != frontend_platform` → `frontend-role-reserved`
   - `platform == frontend_platform and role != "frontend"` → `frontend-must-join-as-frontend`
4. `expected_set` must equal `set(PORTABLE_SEATS)` or return 2.
5. Beacon path uses `announce_portable_beacon` + `PORTABLE_SERVICE_ID` in logs.
6. Plan via `plan_portable_shards` / `PortablePeer` / `shards_to_plan_dict`.
7. After all seats join: `plan_portable_shards` → broadcast plan → wait for frontend `done` or peer `pipeline-error` / disconnect / pipe timeout → `_broadcast_shutdown` → return 0 on success, 1 on any fail. Disconnect detection: `MSG_PEEK` empty read appends `peer-{platform}-disconnected` to `fail_reason`.
8. Display names via `PORTABLE_DISPLAY` (iPhone / Windows).

Signature of `run_hub` must match the Interfaces block exactly (`frontend_platform` is required keyword-only).

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 scripts/portable_dual_topology_selftest.py`

Expected: PASS — `PASS: lib unit checks` and `PASS: hub reject checks`, exit 0

- [ ] **Step 5: Commit**

```bash
git add scripts/portable_dual_topology_hub.py scripts/portable_dual_topology_selftest.py
git commit -m "$(cat <<'EOF'
feat(portable): hub JOIN rejects non-product seats and role mismatch

Enforce ios+windows only and frontend-platform reservation for Phase B.
EOF
)"
```

---

### Task 3: Peer frontend/worker activation stub

**Files:**
- Create: `scripts/portable_dual_topology_peer.py`
- Modify: `scripts/portable_dual_topology_selftest.py`
- Test: `scripts/portable_dual_topology_selftest.py`

**Interfaces:**
- Consumes: Task 1 framing/plan/discovery; Task 2 hub is not imported by peer
- Produces: `run_peer(platform: str, role: str, timeout_s: float, *, usable_gb: float | None = None, discovery_port: int = DISCOVERY_PORT, hub_host: str | None = None, hub_port: int | None = None, prompt: str = "ShareCompute Phase B portable prompt") -> int`

- [ ] **Step 1: Write the failing peer import smoke**

Append to selftest:

```python
def test_peer_module_importable() -> None:
    from portable_dual_topology_peer import run_peer
    assert callable(run_peer)
    print("PASS: peer module import")
```

Call from `run_unit_tests()`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 scripts/portable_dual_topology_selftest.py`

Expected: FAIL with `ModuleNotFoundError: No module named 'portable_dual_topology_peer'`

- [ ] **Step 3: Write minimal implementation**

Create `scripts/portable_dual_topology_peer.py` by forking `scripts/inference_pipeline_peer.py` with these exact differences:

1. Import from `portable_dual_topology_lib` only: `PORTABLE_SEATS`, `PORTABLE_DISPLAY`, `PORTABLE_SERVICE_ID`, `PORTABLE_DEFAULT_USABLE_GB`, `ROLES`, `DEFAULT_ACTIVATION_BYTES`, `DEFAULT_HOST`, `DISCOVERY_MULTICAST_GROUP`, `DISCOVERY_PORT`, `ActivationMsg`, `PortableShard`, `activation_nbytes_for_budget`, `buffer_budget_bytes`, `connect_with_deadline`, `discover_portable_hub`, `fake_compute`, `make_prompt_activation`, `open_data_listener`, `plan_dict_to_shards`, `recv_activation`, `recv_line`, `send_activation`, `send_msg`.
2. Guard at start of `run_peer`:
   ```python
   if platform not in PORTABLE_SEATS:
       print(f"error: non-product platform {platform}", file=sys.stderr)
       return 2
   if role not in ROLES:
       print(f"error: unknown role {role}", file=sys.stderr)
       return 2
   gb = float(usable_gb if usable_gb is not None else PORTABLE_DEFAULT_USABLE_GB[platform])
   ```
3. Discovery: call `discover_portable_hub`; logs must mention `PORTABLE_SERVICE_ID` (not `sharecompute-infer`).
4. Keep Phase A control flow for JOIN → wait PLAN → epoch check → `_run_frontend` / `_run_worker` → `done` / `pipeline-error`.
5. Frontend seeds prompt, fake-computes rank 0 layers, sends SCPT activation to rank 1, accepts final return from last rank, reports `done`.
6. Worker accepts inbound, validates `rank_from`/`rank_to`, fake-computes, forwards to next (last hop returns to frontend rank 0).
7. Default prompt: `"ShareCompute Phase B portable prompt"`.
8. Helpers `_run_frontend`, `_run_worker`, `_wait_shutdown`, `_cleanup` stay private in this module with the same signatures/behavior as Phase A (typed with `PortableShard`).

Do not import `inference_pipeline_*`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 scripts/portable_dual_topology_selftest.py`

Expected: PASS including `PASS: peer module import`

- [ ] **Step 5: Commit**

```bash
git add scripts/portable_dual_topology_peer.py scripts/portable_dual_topology_selftest.py
git commit -m "$(cat <<'EOF'
feat(portable): peer frontend/worker SCPT activation stub

Port Phase A peer pipeline to portable seats and sharecompute-portable discovery.
EOF
)"
```

---

### Task 4: Orchestrator single-topology spawn + demo CLI

**Files:**
- Create: `scripts/portable_dual_topology_orch.py`
- Create: `scripts/portable_dual_topology_demo.py`
- Modify: `scripts/portable_dual_topology_selftest.py`
- Test: `scripts/portable_dual_topology_selftest.py`

**Interfaces:**
- Consumes: `run_hub`, `run_peer`, `resolve_topology`, `PORTABLE_SEATS`, `PORTABLE_DISPLAY`, `PORTABLE_SERVICE_ID`, `TOPOLOGY_CHOICES`, `HUB_READY_PREFIX`, `DEFAULT_*`
- Produces:
  - `run_one_topology(script_path: str, topology: str, host: str, port: int, timeout_s: float, *, fail_platforms: Sequence[str] = (), fail_discovery: bool = False, kill_worker: Optional[str] = None, discovery_port: int = DISCOVERY_PORT, token_count: int = DEFAULT_TOKEN_COUNT, usable_gb: Optional[float] = None, fail_role_mismatch: bool = False) -> int`
  - `run_orchestrator(..., topology: str = "both", ...) -> int`
  - `build_parser() -> argparse.ArgumentParser`
  - `main(argv: Optional[Sequence[str]] = None) -> int`
  - Demo file only calls `main()`

- [ ] **Step 1: Write failing happy-path subprocess tests**

Append to selftest:

```python
def _run_demo(args: list[str], timeout: float = 60.0) -> subprocess.CompletedProcess:
    cmd = [sys.executable, DEMO] + args
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, cwd=os.path.dirname(HERE)
    )


def test_happy_iphone_frontend() -> None:
    if not os.path.isfile(DEMO):
        _fail(f"missing demo entry {DEMO}")
    cp = _run_demo(["--topology", "iphone-frontend", "--timeout", "8", "--token-count", "4"])
    if cp.returncode != 0:
        sys.stderr.write(cp.stdout + "\n" + cp.stderr)
        _fail(f"iphone-frontend expected exit 0, got {cp.returncode}")
    out = cp.stdout + cp.stderr
    assert "iPhone" in out or "ios" in out
    assert "Windows" in out or "windows" in out
    print("PASS: happy iphone-frontend")


def test_happy_windows_frontend() -> None:
    cp = _run_demo(["--topology", "windows-frontend", "--timeout", "8", "--token-count", "4"])
    if cp.returncode != 0:
        sys.stderr.write(cp.stdout + "\n" + cp.stderr)
        _fail(f"windows-frontend expected exit 0, got {cp.returncode}")
    print("PASS: happy windows-frontend")


def run_matrix() -> None:
    test_happy_iphone_frontend()
    test_happy_windows_frontend()
```

Update `main()`:

```python
def main() -> int:
    run_unit_tests()
    if os.path.isfile(DEMO):
        run_matrix()
    print("ALL PASS: portable_dual_topology_selftest")
    return 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 scripts/portable_dual_topology_selftest.py`

Expected: FAIL — missing `portable_dual_topology_demo.py` or non-zero demo exit

- [ ] **Step 3: Write orch + demo**

`scripts/portable_dual_topology_demo.py`:

```python
#!/usr/bin/env python3
"""Phase B — portable dual-topology stub sim entry point.

    python3 scripts/portable_dual_topology_demo.py
    python3 scripts/portable_dual_topology_demo.py --topology iphone-frontend
    python3 scripts/portable_dual_topology_demo.py --topology windows-frontend
    python3 scripts/portable_dual_topology_demo.py --fail-discovery
    python3 scripts/portable_dual_topology_demo.py --fail-platform ios
    python3 scripts/portable_dual_topology_demo.py --kill-worker mid
    python3 scripts/portable_dual_topology_demo.py --usable-gb 0.1
    python3 scripts/portable_dual_topology_demo.py --fail-role-mismatch

See docs/PORTABLE-DUAL-TOPOLOGY-SIM.md.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from portable_dual_topology_orch import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
```

`scripts/portable_dual_topology_orch.py` — fork `scripts/inference_pipeline_orch.py` with:

1. Imports from `portable_dual_topology_lib`, `portable_dual_topology_hub.run_hub`, `portable_dual_topology_peer.run_peer`.
2. `--topology` choices=`TOPOLOGY_CHOICES`, default=`"both"`. For Task 4, implement `run_one_topology` for `iphone-frontend` and `windows-frontend`. Prefer also implementing the `both` branch (sequential A then B, aggregate exit codes) here so the demo default works; if deferred, Task 5 completes it before matrix expansion.
3. `run_one_topology`:
   - Resolve `frontend_platform, worker_platform = resolve_topology(topology)`.
   - Print banner `ShareCompute portable dual-topology sim (Phase B)` plus topology line.
   - Spawn hub: `--role hub --expect ios,windows --frontend-platform {frontend_platform} --token-count ... --discovery-port ...` (+ `--no-beacon` if `fail_discovery`).
   - Wait for `HUB_READY_PREFIX`; fail exit 1 if hub not ready.
   - Build seat list `[(frontend_platform,"frontend"), (worker_platform,"worker")]`.
   - If `fail_role_mismatch`: swap roles on that list.
   - Skip platforms in `fail_platforms`.
   - Spawn peers via same `script_path` (demo entry) with `--role peer --platform --peer-role --timeout --discovery-port` and optional `--usable-gb`.
   - Drain stdout threads; detect `discovery failed` lines.
   - Kill-worker loop: wait for `accepted plan` / `token 0` progress (mid) or brief settle (start); `proc.kill()`; append evidence `SIGKILL-ok:{platform}:pid={pid}:exit={rc}`; if evidence missing or hub rc==0 after kill, return 1; widen `token_count` to ≥64 when killing.
   - `hub.wait(overall_timeout)` where `overall_timeout = timeout_s + max(timeout_s, 15) + (12 if kill_worker else 5)`; hang → terminate → 1.
   - Negative discovery / kill evidence handling same as Phase A; return hub rc otherwise.
4. `build_parser` flags: `--role` (`orchestrator|hub|peer`), `--topology`, `--host`, `--port`, `--timeout`, `--platform` (choices=`PORTABLE_SEATS`), `--peer-role`, `--expect`, `--frontend-platform` (choices=`PORTABLE_SEATS`), `--token-count`, `--fail-platform` (append, choices=`PORTABLE_SEATS`), `--fail-discovery`, `--no-beacon`, `--discovery-port`, `--usable-gb`, `--kill-worker` (`mid|start`), `--skip-discovery`, `--fail-role-mismatch`.
5. `main`: reject `token_count <= 0` with exit 1; dispatch hub/peer/orchestrator.
6. Script path resolution: prefer `portable_dual_topology_demo.py` beside orch (Phase A pattern).

Seat spawn snippet (must appear in orch):

```python
    frontend_platform, worker_platform = resolve_topology(topology)
    seats = [(frontend_platform, "frontend"), (worker_platform, "worker")]
    if fail_role_mismatch:
        seats = [(frontend_platform, "worker"), (worker_platform, "frontend")]
        print("Negative test: --fail-role-mismatch (swapped JOIN roles)", flush=True)
    for platform, role in seats:
        if platform in fail_set:
            print(
                f"  · skipping peer for {PORTABLE_DISPLAY[platform]} (--fail-platform)",
                flush=True,
            )
            continue
        peer_cmd = [
            py, script_path, "--role", "peer",
            "--platform", platform, "--peer-role", role,
            "--timeout", str(timeout_s),
            "--discovery-port", str(discovery_port),
            "--frontend-platform", frontend_platform,
        ]
        if usable_gb is not None:
            peer_cmd.extend(["--usable-gb", str(usable_gb)])
        # Popen + peer_meta append as in Phase A
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 scripts/portable_dual_topology_selftest.py`

Expected: PASS — happy iphone-frontend and windows-frontend

Also:

```bash
python3 scripts/portable_dual_topology_demo.py --topology iphone-frontend --token-count 4
# Expected: exit 0
python3 scripts/portable_dual_topology_demo.py --topology windows-frontend --token-count 4
# Expected: exit 0
```

- [ ] **Step 5: Commit**

```bash
git add scripts/portable_dual_topology_orch.py scripts/portable_dual_topology_demo.py scripts/portable_dual_topology_selftest.py
git commit -m "$(cat <<'EOF'
feat(portable): orch + demo CLI for single-topology happy paths

Spawn hub and ios/windows peers from topology map; demo entry delegates to orch.
EOF
)"
```

---

### Task 5: `--topology both` aggregation

**Files:**
- Modify: `scripts/portable_dual_topology_orch.py`
- Modify: `scripts/portable_dual_topology_selftest.py`
- Test: `scripts/portable_dual_topology_selftest.py`

**Interfaces:**
- Consumes: `run_one_topology` from Task 4
- Produces: `run_orchestrator` with `topology="both"` runs `iphone-frontend` then `windows-frontend` on fresh hub ports; returns 0 iff both return 0; prints `PASS: both topologies succeeded` or `FAIL: topology aggregate iphone-frontend={rc_a} windows-frontend={rc_b}`

- [ ] **Step 1: Write failing both-topology tests**

```python
def test_happy_both() -> None:
    cp = _run_demo(["--topology", "both", "--timeout", "8", "--token-count", "4"], timeout=120.0)
    if cp.returncode != 0:
        sys.stderr.write(cp.stdout + "\n" + cp.stderr)
        _fail(f"both expected exit 0, got {cp.returncode}")
    out = cp.stdout + cp.stderr
    if "PASS: both topologies succeeded" not in out:
        # Still require both topology markers if banner wording differs slightly
        if "iphone-frontend" not in out.lower() and "ios FE" not in out:
            _fail("both run did not log first topology")
    print("PASS: happy both")


def test_both_aggregates_failure() -> None:
    cp = _run_demo(["--topology", "both", "--fail-discovery", "--timeout", "3"], timeout=60.0)
    if cp.returncode == 0:
        _fail("both + fail-discovery must not exit 0")
    print("PASS: both aggregates failure")
```

Add both to `run_matrix()`.

- [ ] **Step 2: Run test to verify fail if both incomplete**

Run: `python3 scripts/portable_dual_topology_selftest.py`

Expected: FAIL if `both` not implemented; otherwise may already PASS (then proceed to commit tests).

- [ ] **Step 3: Implement `both` aggregation**

In `run_orchestrator`:

```python
def run_orchestrator(script_path: str, *, topology: str = "both", host: str, port: int,
                     timeout_s: float, fail_platforms, fail_discovery: bool = False,
                     kill_worker=None, discovery_port: int = DISCOVERY_PORT,
                     token_count: int = DEFAULT_TOKEN_COUNT, usable_gb=None,
                     fail_role_mismatch: bool = False) -> int:
    knobs = dict(
        fail_platforms=fail_platforms,
        fail_discovery=fail_discovery,
        kill_worker=kill_worker,
        discovery_port=discovery_port,
        token_count=token_count,
        usable_gb=usable_gb,
        fail_role_mismatch=fail_role_mismatch,
    )
    if topology == "both":
        print("=== Topology A: iphone-frontend (ios FE + windows worker) ===\n", flush=True)
        rc_a = run_one_topology(script_path, "iphone-frontend", host, 0, timeout_s, **knobs)
        print("\n=== Topology B: windows-frontend (windows FE + ios worker) ===\n", flush=True)
        rc_b = run_one_topology(script_path, "windows-frontend", host, 0, timeout_s, **knobs)
        if rc_a == 0 and rc_b == 0:
            print("\nPASS: both topologies succeeded", flush=True)
            return 0
        print(
            f"\nFAIL: topology aggregate iphone-frontend={rc_a} windows-frontend={rc_b}",
            flush=True,
        )
        return 1
    return run_one_topology(script_path, topology, host, port, timeout_s, **knobs)
```

Ensure `build_parser` default: `p.add_argument("--topology", choices=TOPOLOGY_CHOICES, default="both")`.

Each `run_one_topology` must allocate its own ephemeral hub port when `port==0` (Phase A probe bind pattern) so A and B do not collide.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 scripts/portable_dual_topology_selftest.py`

Expected: PASS including `PASS: happy both` and `PASS: both aggregates failure`

```bash
python3 scripts/portable_dual_topology_demo.py --token-count 4
# Expected: exit 0 (default both)
```

- [ ] **Step 5: Commit**

```bash
git add scripts/portable_dual_topology_orch.py scripts/portable_dual_topology_selftest.py
git commit -m "$(cat <<'EOF'
feat(portable): default --topology both with exit aggregation

Run iphone-frontend then windows-frontend; exit 0 only if both succeed.
EOF
)"
```

---

### Task 6: Fail-modes matrix

**Files:**
- Modify: `scripts/portable_dual_topology_orch.py` (finish any missing knobs)
- Modify: `scripts/portable_dual_topology_selftest.py`
- Test: `scripts/portable_dual_topology_selftest.py`

**Interfaces:**
- Consumes: orch flags `--fail-discovery`, `--fail-platform`, `--usable-gb`, `--fail-role-mismatch`, `--kill-worker`, `--token-count`
- Produces: each forced failure exits ≠ 0; kill cases include `SIGKILL-ok:` evidence in combined stdout

- [ ] **Step 1: Write failing matrix cases**

Extend `run_matrix()` with:

```python
def test_fail_discovery() -> None:
    cp = _run_demo(["--topology", "iphone-frontend", "--fail-discovery", "--timeout", "3"])
    if cp.returncode == 0:
        _fail("fail-discovery must exit != 0")
    print("PASS: fail-discovery")


def test_fail_platform_missing_seat() -> None:
    cp = _run_demo(["--topology", "iphone-frontend", "--fail-platform", "windows", "--timeout", "3"])
    if cp.returncode == 0:
        _fail("missing windows seat must exit != 0")
    print("PASS: fail-platform")


def test_fail_usable_gb() -> None:
    cp = _run_demo(
        ["--topology", "iphone-frontend", "--usable-gb", "0.1", "--timeout", "8", "--token-count", "2"]
    )
    if cp.returncode == 0:
        _fail("usable-gb 0.1 must exit != 0")
    print("PASS: usable-gb reject")


def test_fail_role_mismatch() -> None:
    cp = _run_demo(["--topology", "iphone-frontend", "--fail-role-mismatch", "--timeout", "5"])
    if cp.returncode == 0:
        _fail("role mismatch must exit != 0")
    print("PASS: fail-role-mismatch")


def test_fail_role_mismatch_windows_frontend() -> None:
    cp = _run_demo(["--topology", "windows-frontend", "--fail-role-mismatch", "--timeout", "5"])
    if cp.returncode == 0:
        _fail("role mismatch on windows-frontend must exit != 0")
    print("PASS: fail-role-mismatch windows-frontend")


def test_kill_worker_mid() -> None:
    cp = _run_demo(
        ["--topology", "iphone-frontend", "--kill-worker", "mid", "--timeout", "12"],
        timeout=90.0,
    )
    if cp.returncode == 0:
        _fail("kill-worker mid must exit != 0")
    out = cp.stdout + cp.stderr
    if "SIGKILL-ok:" not in out:
        sys.stderr.write(out)
        _fail("kill-worker mid missing SIGKILL-ok evidence")
    print("PASS: kill-worker mid")


def test_kill_worker_start() -> None:
    cp = _run_demo(
        ["--topology", "windows-frontend", "--kill-worker", "start", "--timeout", "12"],
        timeout=90.0,
    )
    if cp.returncode == 0:
        _fail("kill-worker start must exit != 0")
    out = cp.stdout + cp.stderr
    if "SIGKILL-ok:" not in out:
        sys.stderr.write(out)
        _fail("kill-worker start missing SIGKILL-ok evidence")
    print("PASS: kill-worker start")


def test_bad_token_count() -> None:
    cp = _run_demo(["--token-count", "-1"])
    if cp.returncode == 0:
        _fail("token-count -1 must exit != 0")
    print("PASS: bad token-count")
```

Call all of the above from `run_matrix()` after the happy-path tests.

- [ ] **Step 2: Run test to verify current gaps**

Run: `python3 scripts/portable_dual_topology_selftest.py`

Expected: FAIL on any missing knob until Step 3 completes

- [ ] **Step 3: Complete orch fail knobs**

Verify argparse exposes:

```python
p.add_argument("--fail-role-mismatch", action="store_true")
p.add_argument("--fail-platform", action="append", default=[], choices=PORTABLE_SEATS, dest="fail_platforms")
p.add_argument("--kill-worker", choices=("mid", "start"), default=None)
p.add_argument("--usable-gb", type=float, default=None)
p.add_argument("--fail-discovery", action="store_true")
```

Wire each flag into `run_one_topology` / `run_orchestrator` / `main` exactly as Interfaces describe. Kill path must:

1. Bump `token_count` to at least 64 when `kill_worker` is set.
2. Record `SIGKILL-ok:{platform}:pid=...:exit=...` evidence.
3. If evidence missing or hub returns 0 after kill → orch returns 1.

Role-mismatch: swapped JOIN roles → hub errors → missing seats / join failure → exit 1, no hang.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 scripts/portable_dual_topology_selftest.py`

Expected: PASS — `ALL PASS: portable_dual_topology_selftest`, exit 0

- [ ] **Step 5: Commit**

```bash
git add scripts/portable_dual_topology_orch.py scripts/portable_dual_topology_selftest.py
git commit -m "$(cat <<'EOF'
test(portable): full fail-mode matrix with kill evidence

Cover discovery, missing seat, RAM reject, role mismatch, kill mid/start.
EOF
)"
```

---

### Task 7: Documentation `PORTABLE-DUAL-TOPOLOGY-SIM.md`

**Files:**
- Create: `docs/PORTABLE-DUAL-TOPOLOGY-SIM.md`
- Modify: `scripts/portable_dual_topology_selftest.py`
- Test: `scripts/portable_dual_topology_selftest.py`

**Interfaces:**
- Consumes: public CLI from orch; constants from lib
- Produces: operator doc covering success + fail commands, architecture table, files table, Next→RPC handoff

- [ ] **Step 1: Write failing docs content check**

```python
def test_docs_exist_and_mention_commands() -> None:
    candidates = [
        os.path.normpath(os.path.join(HERE, "..", "docs", "PORTABLE-DUAL-TOPOLOGY-SIM.md")),
        "docs/PORTABLE-DUAL-TOPOLOGY-SIM.md",
    ]
    path = next((c for c in candidates if os.path.isfile(c)), None)
    if path is None:
        _fail("docs/PORTABLE-DUAL-TOPOLOGY-SIM.md missing")
    text = open(path, encoding="utf-8").read()
    for needle in (
        "sharecompute-portable",
        "SCPT",
        "--topology both",
        "--topology iphone-frontend",
        "--topology windows-frontend",
        "--fail-discovery",
        "--fail-platform",
        "--kill-worker mid",
        "--kill-worker start",
        "--usable-gb",
        "--fail-role-mismatch",
        "llama.cpp",
        "portable_dual_topology_selftest.py",
    ):
        if needle not in text:
            _fail(f"docs missing required needle: {needle}")
    print("PASS: docs content")
```

Call from `run_matrix()` (or `run_unit_tests()`).

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 scripts/portable_dual_topology_selftest.py`

Expected: FAIL — `docs/PORTABLE-DUAL-TOPOLOGY-SIM.md missing`

- [ ] **Step 3: Write the doc**

Create `docs/PORTABLE-DUAL-TOPOLOGY-SIM.md`:

```markdown
# Portable dual-topology simulation (Phase B stub)

Pre-device product gate: prove **both** role directions on one portable control +
data plane with **ios** and **windows** seats only. Fake-but-sized compute; not
llama.cpp and not InferRing/MLX.

## What it proves

| Layer | Status in this demo |
|---|---|
| UDP multicast discovery (`239.255.77.77:37777`, service `sharecompute-portable`) | **Real** |
| TCP JOIN with platform + role (`frontend` / `worker`) + usable GB | **Real** |
| Seats gated to **ios** + **windows** (Mac/Android rejected) | **Real** |
| Topology `iphone-frontend` (ios FE + windows worker) | **Real** |
| Topology `windows-frontend` (windows FE + ios worker) | **Real** |
| Default `--topology both` → exit 0 only if both succeed | **Real** |
| StagePlanner-equivalent plan + epoch; unfit RAM rejected | **Real** |
| TCP activation framing magic `SCPT` along shard ranks | **Real** |
| Missing seat / discovery fail / kill worker / role mismatch → exit ≠ 0 | **Real** |
| Real ggml-rpc / llama.cpp / LAN iPhone↔Windows | **Not claimed** |

## How to run

```bash
# Success — both topologies (default)
python3 scripts/portable_dual_topology_demo.py
# equivalent:
python3 scripts/portable_dual_topology_demo.py --topology both

# Success — single topology
python3 scripts/portable_dual_topology_demo.py --topology iphone-frontend
python3 scripts/portable_dual_topology_demo.py --topology windows-frontend

# Negative — hub does not announce
python3 scripts/portable_dual_topology_demo.py --fail-discovery

# Negative — omit a product seat
python3 scripts/portable_dual_topology_demo.py --fail-platform ios
python3 scripts/portable_dual_topology_demo.py --fail-platform windows

# Negative — SIGKILL worker mid / start (requires kill evidence)
python3 scripts/portable_dual_topology_demo.py --kill-worker mid
python3 scripts/portable_dual_topology_demo.py --kill-worker start

# Negative — plan unfit
python3 scripts/portable_dual_topology_demo.py --usable-gb 0.1

# Negative — peers JOIN with swapped roles
python3 scripts/portable_dual_topology_demo.py --fail-role-mismatch

# Argument error
python3 scripts/portable_dual_topology_demo.py --token-count -1

# Automated gate
python3 scripts/portable_dual_topology_selftest.py
```

Optional knobs: `--timeout`, `--token-count` (> 0), `--discovery-port`, `--usable-gb`,
`--skip-discovery` (peer role with `--host`/`--port`).

## Architecture

```
  orch (--topology both runs A then B)
   ├── hub  (service=sharecompute-portable)
   ├── peer frontend platform  role=frontend rank 0
   └── peer worker platform    role=worker   rank 1
        SCPT activation: FE → worker → FE
```

| `--topology` | Frontend | Worker |
|---|---|---|
| `iphone-frontend` | ios | windows |
| `windows-frontend` | windows | ios |
| `both` (default) | both rows in sequence | |

CLI uses product language `iphone-frontend`; wire/platform enum stays `ios`.

## Files

| Path | Role |
|---|---|
| `scripts/portable_dual_topology_demo.py` | CLI entry |
| `scripts/portable_dual_topology_orch.py` | Multi-process orchestrator |
| `scripts/portable_dual_topology_hub.py` | Control-plane hub |
| `scripts/portable_dual_topology_peer.py` | Frontend / worker peers |
| `scripts/portable_dual_topology_lib.py` | Framing, planning, discovery |
| `scripts/portable_dual_topology_selftest.py` | Exit-code matrix gate |
| `scripts/four_platform_pool_lib.py` | Shared multicast + line framing |

Phase A sibling (unchanged): [`INFERENCE-PIPELINE-SIM.md`](INFERENCE-PIPELINE-SIM.md).

## Next (real llama.cpp RPC — not this stub)

Keep seats, topology matrix, hub role rules, and both-topologies success criterion.
Replace `SCPT` fake activation with `Spikes/llamacpp-rpc`. Judge success from
`llama_decode` / compute status — never process exit alone (findings F33–F35).
See spike README + `findings.md` for RPC hazards (F15, F25–F27, F33–F35).
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 scripts/portable_dual_topology_selftest.py`

Expected: PASS including `PASS: docs content`

- [ ] **Step 5: Commit**

```bash
git add docs/PORTABLE-DUAL-TOPOLOGY-SIM.md scripts/portable_dual_topology_selftest.py
git commit -m "$(cat <<'EOF'
docs(portable): PORTABLE-DUAL-TOPOLOGY-SIM success and fail matrix

Document both-topology default, forced failures, and RPC handoff boundary.
EOF
)"
```

---

### Task 8: Verification against success criteria + Phase A unchanged

**Files:**
- Test only (no new production files unless a verification fix is required)
- Verify: spec success criteria 1–4; Phase A demos still pass

**Interfaces:**
- Consumes: all Task 1–7 deliverables
- Produces: recorded command results; no Phase A path modifications

- [ ] **Step 1: Run Phase B success-criteria commands**

```bash
# Criterion 1 — default both exits 0
python3 scripts/portable_dual_topology_demo.py
# Expected: PASS / exit 0; logs both topologies and "PASS: both topologies succeeded"

# Criterion 2 — forced failures exit != 0, no hang
python3 scripts/portable_dual_topology_demo.py --fail-discovery; echo exit=$?
# Expected: exit=1
python3 scripts/portable_dual_topology_demo.py --fail-platform ios; echo exit=$?
# Expected: exit=1
python3 scripts/portable_dual_topology_demo.py --kill-worker mid; echo exit=$?
# Expected: exit=1 with SIGKILL-ok evidence
python3 scripts/portable_dual_topology_demo.py --kill-worker start; echo exit=$?
# Expected: exit=1 with SIGKILL-ok evidence
python3 scripts/portable_dual_topology_demo.py --usable-gb 0.1; echo exit=$?
# Expected: exit=1
python3 scripts/portable_dual_topology_demo.py --fail-role-mismatch; echo exit=$?
# Expected: exit=1

# Criterion 3 — docs exist
test -f docs/PORTABLE-DUAL-TOPOLOGY-SIM.md && echo docs_ok
# Expected: docs_ok

# Full automated gate
python3 scripts/portable_dual_topology_selftest.py
# Expected: ALL PASS, exit 0
```

- [ ] **Step 2: Confirm expected PASS/FAIL outcomes match the matrix**

Re-run any failing command from Step 1 until green. Do not weaken assertions.

- [ ] **Step 3: Phase A regression + untouched check**

```bash
python3 scripts/inference_pipeline_demo.py --token-count 4
# Expected: exit 0

git diff --name-only origin/main...HEAD | rg 'inference_pipeline_|INFERENCE-PIPELINE-SIM' || echo 'Phase A paths untouched'
# Expected: Phase A paths untouched (or empty match)

rg -n 'sharecompute-portable|SCPT|PORTABLE_SEATS' scripts/portable_dual_topology_*.py
rg -n 'sharecompute-infer|SCIN' scripts/inference_pipeline_lib.py
# Expected: portable files use portable constants; Phase A keeps infer/SCIN
```

- [ ] **Step 4: Fix only if verification found a bug, then re-run selftest**

If a fix is required, implement the minimal change, re-run `python3 scripts/portable_dual_topology_selftest.py` (Expected: PASS), then commit:

```bash
git add -u
git commit -m "$(cat <<'EOF'
fix(portable): address verification gaps for success criteria

Ensure both-topology default and fail matrix match the approved spec.
EOF
)"
```

If verification is clean, do not create an empty commit.

- [ ] **Step 5: Record completion**

Note in the PR body: success criteria 1–4 green; Phase A unchanged; selftest ALL PASS.

---

## Execution Handoff

Plan saved for: `docs/superpowers/plans/2026-09-22-portable-dual-topology.md`.

Implement task-by-task with TDD (selftest fail → code → pass → commit). Prefer **subagent-driven-development** so each task gets a fresh implementer + reviewer; **executing-plans** is acceptable for a single long session.

Do not start implementation until a human confirms this plan matches the approved spec.
