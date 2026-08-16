# ShareCompute — project context

Read this before doing anything. It exists so you do not have to re-derive facts that were
expensive to establish, and so you do not repeat mistakes this project has already made.

## What this is

Elastic ring membership for [infer-ring](Apps/InferRing/README.md), following the *Distributed
Heterogeneous Inference Framework* specification.

infer-ring is **N1k1tung's** open-source app — **theirs, not this project's** — shipped by them to
the App Store as `id6757767558`, and vendored into this repository under `Apps/InferRing/`. The
bundle identifiers are `com.n1k1tung.*` and the `DEVELOPMENT_TEAM` values in `project.pbxproj` are
theirs, so **nothing here can be signed or distributed by this project without its own Apple
Developer account and bundle IDs.** Do not infer from the App Store listing that such an account
exists — that mistake has already been made once. It pools RAM across iOS and macOS
devices to run models too large for one machine, using MLX pipeline/tensor parallelism over a
Bonjour-discovered LAN ring. The original failure this project attacks: **one node disappearing
wedged the entire ring indefinitely**, with no detection and no diagnostic.

## Current state

| Piece | Status |
|---|---|
| `ShareComputeCore` — epochs, leases, node state machine, stage planner, watchdog | merged, 67 tests passing |
| Apple adapter — drain-on-background, heartbeats, generation watchdog | compiles in macOS + iOS CI, **never run** |
| Xcode wiring of `ShareComputeCore` as a local package | builds in CI |
| Milestone 2 Stage 1 — MLX ring fails instead of hanging | patch compiles as part of MLX on Apple; harness passes; **never executed** |
| Milestone 2 Stage 2 — group teardown (`finalize()` across mlx, mlx-c, mlx-swift) | 4 patches, compile and **link** on Apple; harness passes; **never executed** |
| Milestone 2 Stage 3 — epoch re-formation in the app | code complete, compiles behind `MLX_HAS_FINALIZE`, **never run** |
| The five patches landed on the forks, project repointed | **both Xcode jobs green** — the patched MLX builds end to end |
| Linux / Windows / Android adapters | **blocked**, see below |

Milestone 2 is code-complete and building. The gap is no longer "uncompiled" — it is **"unrun"**:
no ring has ever formed, `finalize()` has never been called, and no patch has executed against a
real peer. That needs two Apple devices.

Working documents: `task_plan.md` (phases, decisions, errors), `findings.md` (research log, read
this one), `progress.md` (session log, test results).

## Load-bearing facts — do not re-derive these

Established by reading the pinned MLX sources. Full evidence with file and line references is in
`findings.md`.

1. **An MLX `DistributedGroup` could not be torn down or re-initialised.** The fork's `deinit` had
   its only free call commented out, and upstream `distributed::init` caches groups in a
   *function-local static*, returning the stale group on every later call and ignoring the rewritten
   hostfile. This is why membership is modelled as **epochs** rather than mutation — and epochs stay
   the model, because a group still cannot be *mutated*.

   `Patches/` now supplies the missing teardown (`finalize()` across all three repos), so
   destroy-and-rebuild is available to Stage 3. Two constraints come with it: `finalize()` returns
   `false` and changes nothing while any handle is still held, and the loaded model retains the
   group through its sharded layers (F14), so `ModelContext` must be released first.
2. **Every generated token is an all-ranks barrier.** `PipelineLastLayer` runs `allGather` on every
   forward pass. A lease cannot be attached to a rank sitting inside a per-token collective.
3. **An exception must never escape a dispatched MLX task.** `CommandEncoder::dispatch` hands the
   callable to `scheduler::enqueue`; there is no `try`/`catch` anywhere in that path, so an escaping
   exception unwinds a scheduler thread with no handler and calls `std::terminate()`. Failures are
   *recorded* and rethrown later from the caller's own thread.
4. **`std::future::wait()` silently discards an exception.** All ten consumption sites in
   `ring.cpp` used `wait()`. Setting exceptions on promises without also converting every site to
   `get()` produces silent data corruption instead of a hang — worse, because a hang is visible.
5. **`Cmlx` is a source SwiftPM target**, not a prebuilt binary, which is what makes patching MLX
   tractable at all.
6. **mlx-swift vendors its own copies of the mlx-c headers, and those are the only ones Swift
   sees.** The `Cmlx` module map exposes exactly one header, `Source/Cmlx/include/mlx.h`, whose
   include chain reaches `include/mlx/c/distributed_group.h` — a *copy* checked into mlx-swift, not
   the submodule's. Patching the mlx-c submodule compiles the definitions without declaring them.
   Any new C symbol therefore needs a matching declaration in **both** `include/` and
   `include-framework/` (the latter is excluded on Linux only, so it is live on Apple). This cost a
   full CI round to find — see F22.
7. **The app depends on forks**, not upstream, pinned in `project.pbxproj` (not `Package.resolved`):
   - `joeydd032995-pixel/mlx-swift` at branch `sharecompute/free-and-finalize` — **this project's
     patched fork**, carrying all five patches with the `mlx` and `mlx-c` submodules repointed at
     their patched branches. This is what makes `DistributedGroup.finalize()` exist at all.
   - `N1k1tung/mlx-swift-lm` at branch `ios-distrib-0.3.0`, unchanged. That ref is a **tag**, not a
     branch, and SwiftPM resolves it fine — do not "fix" it, and do not conclude it is missing from
     a `git ls-remote --heads` that filters tags out. This has now misled two separate reviews.

   `mlx-swift-lm` declares its own dependency on `ml-explore/mlx-swift`, so the project resolves two
   different URLs under the single SwiftPM identity `mlx-swift` and relies on the **root** pin
   winning. That predates this project (F16) — repointing the root only changed *which* fork wins,
   and CI confirms it resolves.

## Architectural rules

**`Sources/ShareComputeCore/` imports nothing.** Not MLX, not UIKit, not SwiftNIO. Transport is
expressed as a protocol the host implements. This boundary is not stylistic — it is the reason both
MLX spike failures were contained without touching the core, and it is what keeps the
specification's later Linux and Windows adapters reachable without an FFI layer. **Do not add a
dependency to this target.**

**`MembershipService` performs no I/O and owns no timer.** The host reports heartbeat outcomes and
calls `tick(at:)`, so failure detection is a pure function of injected time and is testable without
sleeping or opening a socket.

**iOS keeps `canHostRequiredStage`,** despite specification §12.1 forbidding required stages on
mobile. Pooling an iPhone's RAM with a Mac's *is* the product — it is the README's headline
benchmark. The safety the specification wants comes instead from short OS-clamped leases and
mandatory drain-on-background.

## Why Linux, Windows and Android are blocked

Not sequencing — capability. MLX is Apple-only, so until the specification's Phase 1a (portable
graph IR + wire protocol) and a non-MLX execution path exist, those nodes have no runtime to run.
Any agent asked to build one of those adapters should say so rather than produce code with nothing
to execute it.

## Verification — what can actually be checked here

This container is **x86_64 Linux with no macOS, no Xcode, no Android SDK and no Windows.**

| Work | Verifiable here | How |
|---|---|---|
| `ShareComputeCore` | **yes** | `/opt/swift/usr/bin/swift test` (Swift 6.1.2) |
| MLX C++ patch compiles | **yes** | `g++ -fsyntax-only -std=c++17` with mlx-swift's vendored `json`/`fmt` headers. Proves the submodule TU compiles; says nothing about what mlx-swift *exposes* (F22) |
| New C symbols reachable from Swift | **yes** | a C file including `Source/Cmlx/include/mlx.h`, under `-Werror=implicit-function-declaration` — the flag is load-bearing, plain C passes on an implicit declaration |
| MLX failure semantics | **yes** | `Patches/mlx/tests/socket_thread_failure_test.cpp` against real `socketpair` |
| Agent/skill files — *static* metadata | **yes** | `python3 scripts/validate-agents.py` — parses front matter and checks the mappings |
| Slash commands at **dispatch** | **no** | needs an interactive session: that a fork spawns the named agent, that `background: false` blocks, that a gated command spawns nothing, that `/verify` displaces the built-in |
| Swift syntax of Apple code | partial | `swiftc -parse` — syntax only. It passed the Apple adapter for this project's whole life while three real type errors sat in it (F18) |
| Xcode project builds, patched MLX and all | **not here — but yes in CI** | `.github/workflows/` on a macOS runner. Both jobs green: the patched MLX compiles for `arm64-apple-macos` and iOS Simulator, and the new C symbols link |
| Actor isolation, runtime behaviour | **no** | needs Apple hardware |
| Android / Windows | **no** | needs those SDKs |
| Multi-device ring | **no** | needs two or more real devices |

**State what you verified and what you did not.** Silence about the unverified half is treated as a
defect here, not an omission.

The line has moved, so state it accurately rather than out of habit. The patched MLX now **compiles
and links** as part of the app under a real Apple toolchain, on both macOS and iOS — that claim is
CI-backed and no longer needs hedging. What is still true is that **nothing has been run**: no ring
has ever formed, `finalize()` has never been called, and no MLX patch has executed against a real
peer. Overstating that gap is now as wrong as understating it was.

## Working agreements

- **Stop at a false premise.** Both MLX spikes failed, and the value came from stopping at the gate
  rather than building on the assumption. If a brief rests on something untrue, report it.
- **Prefer the smallest verifiable increment.** Stage 1 shipped alone because it needed no API
  change anywhere else.
- **Do not edit `project.pbxproj` casually.** It is generated; a bad edit leaves a project that will
  not open. Validate structurally before and after.
- **Tests pin defects, not just fixes.** The MLX harness includes a case that mirrors the
  *unpatched* worker and asserts it hangs.

## Agent roster

Role definitions live in `.claude/agents/`. Routing, file ownership and escalation rules are in
`.claude/skills/orchestration/SKILL.md`. Read that before dispatching work to anyone.

Every role also has a slash command at `.claude/skills/<role>/SKILL.md` — `/mac-backend`,
`/tester`, one per agent file. Those are a **human** surface, not a second router: they carry
`disable-model-invocation: true`, so you cannot see them and routing stays one decision in
`orchestration`. The eleven active roles fork the agent directly; the nine gated ones answer inline
and spawn nothing, because a mistyped `agent:` silently resolves to `general-purpose` — inline is the
option that fails *visibly*, not one that restricts tools. **The gate is instructional**, not a
permission boundary: the definitions refuse the work, and all nine gated roles carry `Bash`, so they
were never read-only. See F19.

File ownership lives in `docs/AGENT-OWNERSHIP.md`, outside `.claude/skills/` so a forked role can
read its own contract without reading the router.

Three workflow commands are model-invocable, deliberately: `/verify` (the verification matrix),
`/patches` (the MLX patch set) and `/findings` (append to the research log correctly).

A skill body never restates role knowledge — the agent file is the definition, and a 40-line body
cap in the validator is what keeps that true.

```bash
python3 scripts/validate-agents.py   # 20 agents (9 gated), 24 skills (20 role, 3 workflow, 1 router)
```
