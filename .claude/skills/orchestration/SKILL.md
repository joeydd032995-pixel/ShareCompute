---
name: orchestration
description: Routing, file ownership, isolation and escalation rules for the ShareCompute agent roster. Use when deciding whether to dispatch work to a subagent, which role to dispatch it to, or how to run several agents without colliding. Also use when adding or changing a role definition in .claude/agents/.
---

# Dispatching work on ShareCompute

Read `CLAUDE.md` first. It carries the project facts; this file carries the rules for who does what.

## Before you dispatch anything

**The default is to do the work yourself.** A subagent starts cold and re-derives context you
already hold, which is the expensive path. Dispatch only when one of these is true:

- **Genuine specialisation.** The task needs knowledge you would otherwise have to build from
  scratch — MLX C++ internals, Swift actor isolation, a platform SDK.
- **Genuine parallelism.** Two or more tasks touch disjoint paths and neither blocks the other.
- **Genuine isolation.** The work is exploratory enough that you want it in a worktree.

"This task has several parts" is not a reason to dispatch. Neither is "this looks big".

## Roster

| Role | Owns | Model |
|---|---|---|
| `senior-architect` | the shared contract, spec sequencing, `.claude/**`, `project.pbxproj` | opus |
| `junior-architect` | triage, briefs, `task_plan.md`, `progress.md` | sonnet |
| `mlx-cpp-specialist` | `Patches/mlx/**` | opus |
| `swift-concurrency-specialist` | actor isolation review across Apple code | opus |
| `mac-designer` / `mac-developer` / `mac-backend` | macOS UI / app / adapter | sonnet |
| `ios-designer` / `ios-developer` / `ios-backend` | iOS UI / app / adapter | sonnet |
| `linux-*`, `windows-*`, `android-*` | **gated — see below** | sonnet |
| `tester` | verification, `Tests/**` | sonnet |

### Activation gates

`linux-*`, `windows-*` and `android-*` are **blocked**. MLX is Apple-only, so until the
specification's Phase 1a (portable IR + wire protocol) and a non-MLX execution path exist, those
platforms have no runtime. Dispatching to them produces code nothing can execute.

If asked for one of those adapters, say what the gate is and what would unblock it. Do not write
speculative adapter code against a runtime that does not exist.

### Prefer a specialist over a generalist

When the task is narrower than any role, dispatch the **nearest role plus a narrowing brief** rather
than adding a new file — cgroup memory accounting, `PBXFileSystemSynchronizedRootGroup` semantics,
LiteRT delegate selection. A specialty earns its own definition once it recurs. `mlx-cpp-specialist`
exists because MLX internals came up three times.

## File ownership — one writer per path

This is the rule that makes parallel work safe. The shared contract is the collision point.

| Path | Primary owner | Everyone else |
|---|---|---|
| `Sources/ShareComputeCore/**` | `senior-architect` | read-only; file a change request |
| `Tests/ShareComputeCoreTests/**` | `tester`, `senior-architect` | may add cases, not rewrite existing |
| `Patches/mlx/**` | `mlx-cpp-specialist` | read-only |
| `Apps/InferRing/Ring/**` | `mac-backend` | read-only |
| `Apps/InferRing/InferRing/Services/**` | `mac-developer` | see the shared-tree rule below |
| `Apps/InferRing/InferRing/Screens/**` | `mac-designer` | see the shared-tree rule below |
| `Apps/InferRing/**/*.xcodeproj/**` | `senior-architect` | read-only |
| `CLAUDE.md`, `.claude/**` | `senior-architect` | read-only |
| `task_plan.md`, `progress.md` | `junior-architect` | read-only |
| `findings.md` | anyone | **append only** — never rewrite another agent's finding |

**Never run two agents whose owned paths overlap at the same time.**

### The shared Apple tree

macOS and iOS are **one source tree** behind `#if os(...)`, so "the iOS files" is not a real
partition — a change to `RingHealthMonitor` or `RingManagementView` usually lands on both platforms.
Naming a `*-backend` and a `*-developer` owner per platform for the same directory would have been
fiction.

So the Apple app tree has a **single primary owner per directory**, listed above, and:

- `ios-developer`, `ios-designer` and `ios-backend` work in those same paths for iOS-specific
  changes, but **only when the primary is not running**, and they confine edits to platform
  conditionals wherever practical.
- A change that alters shared behaviour — not just the `#if os(iOS)` branch — goes through the
  primary owner.
- `ios-backend` owns iOS lifecycle *behaviour* (drain-on-background, lease clamping) wherever it
  lives, and will own its own directory once the adapter is factored out.

When this rule starts producing collisions, that is the signal to actually split the tree — raise it
with `senior-architect` rather than working around it.

## Contract changes are a request, not an edit

A platform agent that needs a new `CapabilityProfile` field, a new `NodeState`, or a change to
`StagePlanner` **stops**. It appends the request to `findings.md` — what it needs, why, and what it
tried instead — and returns. The architect makes the change, updates the tests, and re-dispatches.

This is not bureaucracy. `ShareComputeCore` importing nothing is what contained both MLX spike
failures without touching the core, and it is what keeps the specification's later desktop adapters
reachable. Fifteen agents editing it concurrently would destroy that in an afternoon.

## Isolation

- **Implementation agents** run with `isolation: "worktree"`.
- **Read-only agents** — design specs, review, triage — need no isolation.
- Worktrees that end up unchanged are cleaned up automatically; no need to manage that.

## Escalation

**Stop at a false premise.** If a brief rests on something untrue, report it instead of working
around it. Both MLX spikes failed, and the value came from stopping at the gate — building on the
assumption would have produced a patch that crashed a shipping app.

Escalate to `senior-architect` when: the task needs a contract change; two roles both appear to own
the path; the work would need a dependency added to `ShareComputeCore`; or the finding invalidates
something in `task_plan.md`.

## Every agent reports verification honestly

Every definition carries the verification matrix from `CLAUDE.md`. The rule is the same for all of
them: **state what you verified and what you did not.** This container has no macOS, no Xcode, no
Android SDK and no Windows, so most platform work here can be written but not built. An agent that
returns Apple code without saying it was never type-checked has failed the task, however good the
code is.

## Worked examples

**"Add `mlx_distributed_group_free` to mlx-c."** → `mlx-cpp-specialist`, worktree. Owns
`Patches/mlx/**`. No contract change. Verifiable here by `g++ -fsyntax-only` plus the socket harness.

**"The `@MainActor` boundaries in `RingHealthMonitor` may not compile."** → `swift-concurrency-specialist`,
read-only first. It can reason about isolation and run `swiftc -parse`, but **cannot** type-check —
that needs a Mac. Expect a report, not a fix that claims to be verified.

**"Build the Android adapter."** → **refuse and explain the gate.** No runtime exists for it yet.
The unblocking work is Phase 1a, which belongs to `senior-architect`.

**"Split the layer planner so each platform can tune it."** → `senior-architect` only. It is the
shared contract; no platform agent may touch it.
