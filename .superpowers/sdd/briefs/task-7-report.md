# Task 7 report

- Status: complete. Updated the portable topology runbook for `stub` vs `llamacpp-rpc`, BIN/model aliases, #26724 probing, same-host/no-auth-TLS/one-worker limits, and backend-specific kill/restart behavior.
- Selftest doc needles now require `--backend`, `llamacpp-rpc`, and `SHARECOMPUTE_LLAMA_BIN`.
- Commit: `70df3eedf7d88d24dba8497b143a067620ed85dd` (`docs: portable RPC same-host gate runbook`).
- Tests: `python3 scripts/portable_rpc_selftest.py` PASS; `python3 scripts/portable_dual_topology_selftest.py` PASS.
- Concern: BIN-gated RPC subprocess matrix SKIPPED because `SHARECOMPUTE_LLAMA_BIN`/`MODEL` are unset; always-on RPC units and the full stub matrix passed.

- Follow-up: clarified the kill matrix so stub kills are non-zero/no-restart, while RPC kills SIGKILL `ggml-rpc-server`, restart once, and exit according to attempt 2.
- Verification: `python3 scripts/portable_dual_topology_selftest.py` PASS; `python3 scripts/portable_rpc_selftest.py` PASS (BIN-gated RPC subprocess matrix skipped because BIN/model are unset).
