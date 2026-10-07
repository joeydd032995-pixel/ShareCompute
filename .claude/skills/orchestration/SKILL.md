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

### That bar governs *your* decision, not the operator's

Every role also has a slash command — `/mac-backend`, `/tester`, one per file in
`.claude/agents/`. A human typing one **has already made the dispatch decision**, so the bar above
does not apply to it and you should not re-litigate it. The two mechanisms are deliberately
separate:

| | Decides | Visible to you |
|---|---|---|
| This skill | whether *you* dispatch, and to whom | yes — it is the router |
| `/<role>` | nothing; the operator already chose | **no** — the commands are `disable-model-invocation: true` |

That is why 20 commands do not compete with this file for routing: you cannot see them. Routing
stays one decision in one place.

Three commands you *can* see — `verify`, `patches` and `findings` — are workflow skills, not roles.
They dispatch nobody, so they are not a routing choice: use them for the procedure they carry, in
your own context or a subagent's.

Two consequences worth knowing. An active role command **forks** — the agent definition becomes the
subagent's system prompt, so the command carries dispatch mechanics only and never role knowledge.
Every role command forks today. A *gated* role command would instead answer **inline** and spawn
nothing, because fork resolution falls back to `general-purpose` on a bad agent name and that
fallback is silent. Inline is not a tool restriction — the body runs with the caller's toolset — it
is the option that fails **visibly**. A gate is instructional: the definition refuses the work. See
`findings.md` F19. `linux-*`, `windows-*` and `android-*` were gated this way until the native split
ran on those platforms (F36, F37).

## Roster

| Role | Owns | Model |
|---|---|---|
| `senior-architect` | the shared contract, spec sequencing, `.claude/**`, `project.pbxproj` | opus |
| `junior-architect` | triage, briefs, `task_plan.md`, `progress.md` | sonnet |
| `mlx-cpp-specialist` | `Patches/mlx/**` | opus |
| `swift-concurrency-specialist` | actor isolation review across Apple code | opus |
| `mac-designer` / `mac-developer` / `mac-backend` | macOS UI / app / adapter (Infer Ring, MLX) | sonnet |
| `ios-designer` / `ios-developer` / `ios-backend` | iOS UI / app / native and lifecycle — ComputeWorker and Infer Ring | sonnet |
| `linux-backend` | the native split runtime, `native/split/**`, on every platform | sonnet |
| `linux-developer` | the laptop coordinator, relay and proof gate, `split_cluster.py` | sonnet |
| `linux-designer` | operator-facing terminal, log and report text | sonnet |
| `windows-developer` | the test kit, `split_launcher.py` and its packaging | sonnet |
| `windows-designer` | the test kit's browser dashboard and first run on Windows | sonnet |
| `windows-backend` | the native runtime on the Windows laptop: build, memory, speed | sonnet |
| `android-developer` / `android-designer` / `android-backend` | Android worker app / its screen and notification / its JNI and native side | sonnet |
| `tester` | verification, `Tests/**`, `docs/evidence/**` | sonnet |

**Where the current objective lives.** The three-device split is `linux-*`, `windows-*`, `android-*`
and the ComputeWorker half of `ios-*`. `mac-*`, `mlx-cpp-specialist`, `swift-concurrency-specialist`
and the Infer Ring half of `ios-*` maintain the MLX path, which CI keeps as regression protection.

### Activation gates

**No role is gated.** `linux-*`, `windows-*` and `android-*` were blocked while MLX was the only
runtime. llama.cpp RPC is now the runtime on all of them: it ran across a laptop, an Android phone
and an iPhone (F37).

What is still unbuilt is the specification's **membership adapter** (§12.2, §12.4, §12.5) on those
platforms. The split runs without it, discarding a failed generation instead of re-forming. If asked
for one, it is real work now, but it touches the contract. Start with `senior-architect`, not the
platform role.

If a gate is ever needed again, mark the role GATED and add its prefix to `GATED_PREFIXES` in
`scripts/validate-agents.py`. Its command then answers inline and the lint enforces both.

### Prefer a specialist over a generalist

When the task is narrower than any role, dispatch the **nearest role plus a narrowing brief** rather
than adding a new file — cgroup memory accounting, `PBXFileSystemSynchronizedRootGroup` semantics,
Android Doze behaviour. A specialty earns its own definition once it recurs. `mlx-cpp-specialist`
exists because MLX internals came up three times.

## File ownership — one writer per path

**The table lives in [`docs/AGENT-OWNERSHIP.md`](../../../docs/AGENT-OWNERSHIP.md)**, along with the
shared-Apple-tree rule and the contract-change protocol. It sits outside `.claude/skills/` on
purpose: a forked role needs the ownership contract but not this router, and a skill should not have
to read a peer skill to find its own boundaries.

Two rules from it bear directly on dispatch, so they are worth repeating here:

- **Never run two agents whose owned paths overlap at the same time.** That is what the table is for.
- **`Sources/ShareComputeCore/**` belongs to `senior-architect` alone.** Every other role that needs
  a contract change appends the request to `findings.md` and returns. `ShareComputeCore` importing
  nothing is what contained both MLX spike failures without touching the core; twenty agents editing
  it concurrently would destroy that in an afternoon.

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
Android SDK and no Windows, so most platform work here can be written but not built. CI builds it,
and only the operator's devices run it on hardware. An agent that
returns Apple code without saying it was never type-checked has failed the task, however good the
code is.

## Worked examples

**"Add `mlx_distributed_group_free` to mlx-c."** → `mlx-cpp-specialist`, worktree. Owns
`Patches/mlx/**`. No contract change. Verifiable here by `g++ -fsyntax-only` plus the socket harness.

**"The `@MainActor` boundaries in `RingHealthMonitor` may not compile."** → `swift-concurrency-specialist`,
read-only first. It can reason about isolation and run `swiftc -parse`, but **cannot** type-check —
that needs a Mac. Expect a report, not a fix that claims to be verified.

**"The Android worker disconnects when the phone locks."** → `android-developer`, worktree. Owns
`WorkerService.java`. Verifiable here only by `javac`. The APK is CI's, and the lock behaviour is a
physical-phone test the operator runs. Say both.

**"Add a field to the worker hello."** → sequence, don't parallelise. `linux-developer` first
(`split_cluster.py` plus a test), then `android-developer` and `ios-developer`. It is one protocol
in three languages.

**"Build the Android membership adapter."** → `senior-architect` first. The runtime exists now, but
wiring `ShareComputeCore` into a non-Swift app is a contract and architecture decision before it is
platform work.

**"Split the layer planner so each platform can tune it."** → `senior-architect` only. It is the
shared contract; no platform agent may touch it.
