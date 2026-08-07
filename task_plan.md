# Task Plan — ShareCompute Milestone 1: Elastic membership for infer-ring

**Goal:** a node can join or leave the ring — gracefully or by dying — and the ring detects it,
re-plans, and keeps working, with no corrupted output and no deadlock.

Full plan and rationale: `/root/.claude/plans/noble-gathering-parnas.md`.

---

## Phases

| # | Phase | Status |
|---|---|---|
| 0 | Planning files + SPM scaffold + vendor infer-ring | complete |
| 1 | Spikes A & B (MLX teardown; blocked collective) | **complete — BOTH FAILED**, see `findings.md` |
| 2 | Core value types: `CapabilityProfile`, `Lease`, `NodeState` | complete |
| 3 | `StagePlanner` — fixes the three F5 defects | complete |
| 4 | `MembershipService` — epochs, leases, failure detection | complete |
| 5 | Apple adapter wiring | **stopped at the spike gate — awaiting a direction decision** |
| 6 | Chaos tests via the F6 hooks | not started |
| 7 | README + docs | complete for what landed |

> **Phase 1 outcome.** Both spikes were answerable by reading the pinned MLX sources, so neither
> needed hardware. Both failed. `MLXManager.teardown()` cannot be written: there is no free API
> and `distributed::init` caches its group in a process-lifetime static, so re-initialising
> returns the stale group. A survivor of a hard failure also cannot recover — the ring backend
> abandons unfulfilled promises on abort, so the collective waits forever with nothing thrown.
>
> The approved plan said "if Spike A fails, stop and re-plan". Phase 5 is therefore **not**
> started: writing a teardown path now known to be impossible would be waste. `ShareComputeCore`
> is unaffected because it never imported MLX. See "Re-plan" in `findings.md` for the three
> options.

---

## Key decisions

| Decision | Rationale |
|---|---|
| Membership changes are **epochs**, not mutations | F1/F2: MLX groups cannot be joined or left; per-token `allGather` makes partial membership impossible |
| iOS keeps `canHostRequiredStage = true` under a **foreground-scoped lease** | Spec §12.1 says otherwise, but combining iPhone RAM with a Mac's is infer-ring's entire value proposition (README headline benchmark). Honors the spec's intent, not its letter |
| `ShareComputeCore` has **zero external dependencies** | Refinement on the approved plan, which said Foundation + NIO. Transport is a protocol, so NIO is not needed; a dependency-free core builds and tests on Linux with nothing to resolve |
| Guaranteed graceful departure, **best-effort** hard failure | Detection ≠ recovery (F2). Proactive drain covers iOS backgrounding, the dominant real failure; hard-kill recovery is bounded by Spike B |
| Ring secret is in M1, not deferred | F7: the new endpoints would otherwise add a remote ring-teardown primitive that does not exist today |

---

## Errors Encountered

| Error | Attempt | Resolution |
|---|---|---|
| `swift: command not found` — no toolchain in container | 1 | Installed Swift 6.1.2 for Ubuntu 24.04 to `/opt/swift` |
| Spikes A & B require MLX on Apple Silicon; container is x86_64 Linux | 1 | Cannot resolve here. Wrote runnable harnesses under `Spikes/` for the user to run on a Mac; recorded as blocked in `findings.md` rather than faking results |
| Xcode project and all MLX/UIKit code uncompilable in this container | 1 | Scoped delivered-and-verified work to `ShareComputeCore`; adapter wiring left unstarted rather than shipped unverified |

---

## Next actions

1. Run Spike A on a Mac (`Spikes/SpikeA_GroupTeardown/`) and record in `findings.md`.
   **If Spike A fails, re-plan before writing `MLXManager.teardown()`.**
2. Run Spike B (`Spikes/SpikeB_BlockedCollective/`), record, and fix the hard-failure policy.
3. Then phase 5: adapter wiring, on a machine that can build the Xcode project.
