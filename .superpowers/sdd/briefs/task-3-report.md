# Task 3 Report: CLI `--backend` + missing-BIN loud fail

**Date:** 2026-09-22 (America/Chicago)  
**Branch:** `feature/llamacpp-rpc-same-host`  
**Commit:** `d42709d` — `feat(portable-rpc): --backend flag and missing-BIN fail`  
**Author (env-only):** joeydd032995-pixel \<joeydd032995-pixel@users.noreply.github.com\>

## Summary

Wired `--backend` (`BACKEND_CHOICES`, default `DEFAULT_BACKEND=stub`) into orch/demo. Orchestrator with `--backend llamacpp-rpc` resolves BIN via `resolve_llama_bin()`, probes with `probe_llama_bin()`, and exits 1 with clear stderr on missing/bad BIN — no silent stub fallback. Stub path unchanged except logging `Backend: stub`. Full RPC generate-after-plan left for Task 4.

## TDD

### RED (Step 1–2)

1. Added `test_backend_rpc_missing_bin_exits_nonzero()` to `scripts/portable_rpc_selftest.py` (verbatim from brief); called from `run_unit_tests()` → `main()` always.
2. Pre-implementation run: test returned **PASS** for the wrong reason (false GREEN).
   - Demo rejected unknown `--backend` with argparse exit 2.
   - stderr contained `llamacpp-rpc`, so `"llama" in out.lower()` matched without a real BIN probe.
   - Documented as brief-test substring false positive; continued to implement the intended fail path.

### GREEN (Step 3–4)

1. `build_parser()`: `--backend` choices=`BACKEND_CHOICES`, default=`DEFAULT_BACKEND`.
2. `main()`: when role orchestrator + backend `llamacpp-rpc`:
   - missing BIN → `error: --backend llamacpp-rpc requires SHARECOMPUTE_LLAMA_BIN or BIN` → exit 1
   - bad BIN → `error: {RpcProbeError}` → exit 1
3. Threaded `backend` into `run_orchestrator` / `run_one_topology`; logs `Backend: {backend}`.
4. Demo module docstring: `--backend stub` / `--backend llamacpp-rpc` examples.

Post-fix verification:

| Case | rc | stderr |
|------|----|--------|
| `--backend llamacpp-rpc`, no BIN/SHARECOMPUTE_LLAMA_BIN | 1 | requires SHARECOMPUTE_LLAMA_BIN or BIN |
| `--backend llamacpp-rpc`, BIN=/tmp/missing… | 1 | llama bin dir missing or not a directory |
| `--help` | 0 | shows `{stub,llamacpp-rpc}` |

## Tests

```bash
python3 scripts/portable_rpc_selftest.py          # ALL PASS (incl. missing BIN loud fail)
python3 scripts/portable_dual_topology_selftest.py # ALL PASS
```

Both exit 0.

## Files changed (committed)

- `scripts/portable_dual_topology_orch.py` — `--backend`, probe, thread backend
- `scripts/portable_dual_topology_demo.py` — docstring examples
- `scripts/portable_rpc_selftest.py` — subprocess missing-BIN test

Not committed (per constraints): `.superpowers/`, `docs/superpowers/`, `__pycache__/`.  
Not modified: Phase A `inference_pipeline_*`.

## Concerns

1. **False RED:** brief assertion `"llama" in out.lower()` matches argparse mentioning `llamacpp-rpc`; pre-impl “PASS” was spurious. Post-impl path is correct (exit 1 + explicit BIN message). Optional follow-up: assert `"requires"` / `"SHARECOMPUTE_LLAMA_BIN"` or `"BIN"` specifically.
2. **Task 4 not started:** `--backend llamacpp-rpc` with a valid BIN still runs the stub data plane; only early probe/fail is in scope here.
3. No push performed.

## Checklist vs brief

- [x] Step 1: Failing subprocess test added, called from main path always  
- [x] Step 2: Run (false GREEN noted; intended RED blocked by substring)  
- [x] Step 3: Wire CLI + probe + thread backend + demo docstring  
- [x] Step 4: Both selftests PASS  
- [x] Step 5: Commit with brief message; identity via env only; no push  

## Review fix

Hardened `test_backend_rpc_missing_bin_exits_nonzero()` so it requires exit code 1
and a meaningful missing-BIN message. Removed the bare `"llama" in out.lower()`
acceptance that allowed argparse/help output to pass the test. Environment clearing
for `SHARECOMPUTE_LLAMA_BIN` and `BIN`, and the demo invocation with
`--backend llamacpp-rpc`, remain unchanged.
