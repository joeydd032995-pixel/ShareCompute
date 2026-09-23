# Task 1 Report: Lib helpers — backend choices, BIN/model, RPC ports

## Status
DONE

## TDD evidence

### RED
Command:

```bash
python3 scripts/portable_rpc_selftest.py
```

Result: failed as expected before implementation with the missing helper constant:

```text
Traceback (most recent call last):
  File ".../scripts/portable_rpc_selftest.py", line 73, in <module>
    raise SystemExit(main())
  ...
  File ".../scripts/portable_rpc_selftest.py", line 22, in test_backend_constants
    assert lib.BACKEND_CHOICES == ("stub", "llamacpp-rpc")
AttributeError: module 'portable_dual_topology_lib' has no attribute 'BACKEND_CHOICES'
```

### GREEN
Commands:

```bash
python3 scripts/portable_rpc_selftest.py
python3 scripts/portable_dual_topology_selftest.py
```

Result: both passed:

```text
PASS: backend constants
PASS: resolve bin/model
PASS: rpc_port_base
ALL PASS: portable_rpc_selftest (units)
PASS: docs content
ShareCompute portable dual-topology sim (Phase B) — control-plane hub
=====================================================================
Listening on 127.0.0.1:58001  timeout=3.0s
Expecting platforms: ios, windows
Frontend seat: iPhone  tokens=2
Beacon: DISABLED (--no-beacon / --fail-discovery)

HUB_READY 58001
  ✗ reject JOIN: non-product platform 'macos'
  ✗ reject JOIN: Windows claimed frontend (reserved for iPhone)
  ✗ reject JOIN: iPhone must JOIN as frontend (got role=worker)

FAIL: platform(s) did not join within deadline:
  ✗ iPhone — no TCP join
  ✗ Windows — no TCP join

Discovery note: hub beacon was disabled — peers likely failed UDP discovery on 239.255.77.77:37777.

Demo failed: 2 of 2 required seat(s) missing.
PASS: hub reject checks
ShareCompute portable dual-topology sim (Phase B) — control-plane hub
=====================================================================
Listening on 127.0.0.1:41083  timeout=2.0s
Expecting platforms: ios, windows
Frontend seat: iPhone  tokens=1
Beacon: DISABLED (--no-beacon / --fail-discovery)

HUB_READY 41083
  ✓ iPhone JOIN role=frontend node=sim-ios usable=6GB data=127.0.0.1:19101 from 127.0.0.1:59784
  ✓ Windows JOIN role=worker node=sim-windows usable=16GB data=127.0.0.1:19102 from 127.0.0.1:59790

  All seats joined. Broadcasting shard plan + epoch.

RAM pool ready — 22.0 GB across iPhone, Windows
Epoch a75fd78b3bf5  model demo-32L  tokens 1

Shard plan (inference pipeline ranks):
  rank 0  iPhone   role=frontend sim-ios  layers [0,8)  ~3.50 GB  data=127.0.0.1:19101
  rank 1  Windows  role=worker   sim-windows  layers [8,32)  ~9.50 GB  data=127.0.0.1:19102

  ✗ reject windows done: unexpected-done-from-windows

FAIL: pipeline error — unexpected-done-from-windows
  ! client ('127.0.0.1', 59784) error: [Errno 9] Bad file descriptor
PASS: peer module import
PASS: lib unit checks
PASS: happy iphone-frontend
PASS: happy windows-frontend
PASS: happy both
PASS: both aggregates failure
PASS: fail-discovery
PASS: fail-platform
PASS: usable-gb reject
PASS: fail-role-mismatch
PASS: fail-role-mismatch windows-frontend
PASS: kill-worker mid
PASS: kill-worker start
PASS: bad token-count
ALL PASS: portable_dual_topology_selftest
```

The dual-topology selftest also exercised its expected negative-path diagnostic output while returning success.

## Files changed

- `scripts/portable_dual_topology_lib.py`
  - Added `BACKEND_CHOICES`, `DEFAULT_BACKEND`, and `DEFAULT_RPC_MODEL_NAME`.
  - Added environment resolution helpers with the specified `SHARECOMPUTE_*` then legacy fallback precedence.
  - Added spike-compatible `rpc_port_base(pid)` below the host ephemeral range.
  - Added the required `os` import.
- `scripts/portable_rpc_selftest.py`
  - Added the exact unit selftest for backend constants, BIN/model resolution, and RPC port selection.

## Self-review

- Implemented only the interfaces and values specified in the task brief.
- Preserved the default backend as `stub` and the existing seats/service identifiers.
- Did not modify Phase A scripts or inference pipeline files.
- Did not add RPC processes or start Task 2.
- `git diff --check` passed.
- Only the two task files are intended for the commit; pre-existing controller scratch files and generated `scripts/__pycache__/` remain untracked and excluded.

## Commit

6eb197a feat(portable-rpc): lib backend/BIN/model/port helpers


## Important-finding follow-up

### TDD RED

Added a regression assertion that simulates `/proc/sys/net/ipv4/ip_local_port_range` beginning at `10064` and requires `rpc_port_base(pid) + 64 < 10064`. Before the implementation fix, `python3 scripts/portable_rpc_selftest.py` failed at that assertion because the old `max(10000, ...)` floor returned `10000`.

### Fix

`rpc_port_base` now caps the deterministic candidate at `ephemeral_lo - 65`, preserving the normal Linux 32768+ behavior while guaranteeing the full 65-port block is strictly below lower configured ephemeral ranges. `/proc` parsing also tolerates malformed values, and the resolver selftest restores its environment variables. Removed unused `subprocess` and `_fail` from `portable_rpc_selftest.py`.

### GREEN

Commands and results:

```text
python3 scripts/portable_rpc_selftest.py        PASS (all units)
python3 scripts/portable_dual_topology_selftest.py  PASS (all checks)
git diff --check                              PASS
```


## Important-finding follow-up: low ephemeral rejection and discovery-port regression

### TDD RED

Added regression coverage for `/proc` lower bounds `65`, `1`, `0`, and `-1`; before the fix, `python3 scripts/portable_rpc_selftest.py` failed because `rpc_port_base(12345)` returned a base instead of raising for `65`.

### Fix

`rpc_port_base` now raises `ValueError` for malformed or non-positive `/proc` lower bounds and whenever the lower bound cannot fit a complete positive 65-port block below it. It retains the normal Linux default and the `10064` behavior, and the regression test verifies the returned block does not contain discovery port `37777`.

### GREEN

```text
python3 scripts/portable_rpc_selftest.py             PASS
python3 scripts/portable_dual_topology_selftest.py   PASS
git diff --check                                   PASS
```

Task 2 was not started.


## Important-finding follow-up: empty ephemeral range rejection

### TDD RED

Added regression coverage for empty and whitespace-only `/proc/sys/net/ipv4/ip_local_port_range` content. Before the implementation fix, `python3 scripts/portable_rpc_selftest.py` failed because empty content fell back to `32768`.

### Fix

`rpc_port_base` now distinguishes an `OSError` (which retains the allowed `32768` fallback) from successfully read but empty/whitespace-only content, which raises `ValueError`.

### GREEN

```text
python3 scripts/portable_rpc_selftest.py             PASS
python3 scripts/portable_dual_topology_selftest.py   PASS
git diff --check                                   PASS
```

Task 2 was not started.
