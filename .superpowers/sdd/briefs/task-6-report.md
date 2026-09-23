# Task 6 Report: RPC selftest matrix

**Date:** 2026-09-22 (America/Chicago)  
**Branch:** `feature/llamacpp-rpc-same-host`  
**Commit:** `a0bbbc1` — `test(portable-rpc): BIN-gated matrix + always-on units`  
**Author (env-only):** joeydd032995-pixel <joeydd032995-pixel@users.noreply.github.com>

## Summary

- Kept portable RPC unit checks always-on and moved the missing-BIN subprocess assertion into `main()`.
- Added BIN-gated happy-path coverage for `iphone-frontend`, `windows-frontend`, and `both`.
- Wired BIN-gated kill-mid and kill-start restart checks into the same matrix.
- Stub selftest remains unchanged and green.

## Tests

```text
python3 scripts/portable_dual_topology_selftest.py  PASS
python3 scripts/portable_rpc_selftest.py            PASS
```

The RPC BIN-gated matrix was skipped because this host has no configured `SHARECOMPUTE_LLAMA_BIN`/`MODEL` environment. No push performed; Task 7 not started.

## Review follow-up

- Hardened both kill tests to require exactly one `restarting topology` log and `SIGKILL-ok:` evidence for either exit status.
- Nonzero exits remain accepted as loud post-restart failures; silent attempt-1 success is rejected.
