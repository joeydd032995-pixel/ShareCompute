# ShareCompute

Elastic ring membership for [infer-ring](Apps/InferRing/README.md) — the first milestone of the
*Distributed Heterogeneous Inference Framework* specification.

infer-ring pools RAM across iOS and macOS devices to run models too large for any single machine.
Today, **one node disappearing takes down the whole ring**: there is no failure detection, no
lease, and no lifecycle handling, so backgrounding an iPhone mid-generation hangs every other
device indefinitely.

This repository adds the membership layer that makes departure a planned event.

## Layout

| Path | What it is |
|---|---|
| `Sources/ShareComputeCore/` | Platform-neutral membership core. **Zero dependencies** — not MLX, not UIKit, not NIO. |
| `Tests/ShareComputeCoreTests/` | 66 tests, no network and no sleeping. |
| `Apps/InferRing/` | The vendored infer-ring app, unmodified. The first adapter. |
| `Patches/` | Patches to MLX, mlx-c and mlx-swift. Start at [`Patches/README.md`](Patches/README.md). |
| `findings.md` | Research log, including both spike results. Read this first. |
| `task_plan.md` | Phase status and decisions. |
| `progress.md` | Session log and test results. |

## Status

**Detection ships. Re-formation is written but has never run.**

| Milestone | State |
|---|---|
| M1 — detection without re-formation | merged |
| M2 Stage 1 — a departing peer fails instead of hanging | patch written, 16 harness checks |
| M2 Stage 2 — the group can be torn down and rebuilt | patches written, 27 harness checks |
| M2 Stage 3 — epoch re-formation in the app | code complete, **never run** |

None of the patches has been built as part of MLX or run on Apple hardware, and no ring has ever
re-formed. Stage 3's core half is covered by tests; its adapter half type-checks in CI and nothing
more. See [`Patches/README.md`](Patches/README.md).

### How this got here

The plan gated adapter work on two spikes. Both were answerable by reading the pinned MLX sources,
and **both failed**:

- **An MLX `DistributedGroup` cannot be torn down or re-initialised.** The fork's `deinit` has its
  `mlx_distributed_group_free` call commented out, and upstream `distributed::init` caches the
  group in a function-local static — so every subsequent init returns the *stale* group, ignoring
  the rewritten hostfile.
- **A survivor cannot escape a blocked collective.** On abort, the ring backend's socket worker
  returns while leaving outstanding `std::promise<void>`s unfulfilled. No exception is thrown, so
  nothing reaches Swift, and the collective waits forever.

Full evidence with file and line references is in [`findings.md`](findings.md).

Writing `MLXManager.teardown()` was therefore not a hard task — it was an *unavailable operation*,
and building it would have been waste. `ShareComputeCore` was unaffected because it never imported
MLX; that boundary is what contained the failure. Milestone 1 shipped detection alone, and milestone
2 went after the constraint itself: `Patches/mlx/0002` supplies the `finalize()` that Spike A found
missing, so the operation now exists and Stage 3 can be written against it.

### What is implemented

Milestone 1's achievable goal was to **turn an indefinite silent hang into a reported failure**,
because the ring could not then be rebuilt. Stage 2 removed that constraint and Stage 3 uses it: a
confirmed loss now moves the ring to `reforming`, the host tears the group down and re-initialises at
a new epoch, and only a *failed* rebuild is terminal. Re-formation is bounded by both a timeout and
an attempt limit — an unbounded retry would be the original hang under a friendlier name.

The detection layer underneath is unchanged and still does the work:

- **iOS announces departure before the OS suspends it.** `willResignActive` fires a fire-and-forget
  `/drain` to every peer with a 2s timeout. Telling some peers and being suspended beats waiting on
  a slow one and telling nobody.
- **Heartbeats actually run.** `DataClient.ping()` and the `/ping` route both already existed and
  were called from nowhere. They now drive `MembershipService`, with three consecutive misses
  before eviction so one dropped Wi-Fi packet does not re-plan the ring.
- **Discovery keeps running.** `RingCoordinator` used to call `bonjourClient?.stopSearching()` the
  moment the MLX ring came up — it stopped watching topology at exactly the moment inference began.
- **A generation that can never finish is abandoned.** `RingWatchdog` runs alongside the stream,
  because the stream cannot report this itself: `for try await` never resumes when MLX is wedged.
  On loss the continuation finishes with `ModelManagerError.ringLost`, naming the device.
- **New requests fail fast** once the ring is dead, instead of hanging too.

A rank wedged *before* Stage 1's patch stays wedged until the process exits. `Patches/mlx/0001`
converts that into a thrown error instead, and Stage 3 rebuilds around it — but neither patch has
been executed, so on an unpatched MLX the old behaviour is still what you get.

A stall alone is **never** treated as loss. Only a stall *plus* known membership loss is. A large
prefill on a phone legitimately produces nothing for many seconds, and aborting that would be a
regression.

### Endpoint authentication

`/drain` accepts only *self*-announcements: the claimed node name must match the peer resolved from
the connection's remote address, reusing the pattern already in `handleModelLoadRequest`. Without
that check the endpoint would let any device on the LAN declare some *other* node dead and force
the ring into a failed state on demand. Broader authentication of the control plane — which is
entirely unauthenticated today, and broadcasts full conversation history to every peer in cleartext
— remains open work.

### Xcode integration

`ShareComputeCore` is wired into the Infer Ring app target as a local Swift package
(`XCLocalSwiftPackageReference`, `relativePath = "../.."` — the repository root, resolved from the
directory holding the `.xcodeproj`). The `Ring` framework target does not reference it and does not
need to.

Individual source files did not need adding: the project uses `objectVersion = 77` with
`PBXFileSystemSynchronizedRootGroup`, so everything under `InferRing/` and `Ring/` is discovered
from the filesystem automatically.

**This is now confirmed working.** The first macOS CI build resolved the reference and produced
`Linking ShareComputeCore.o`, with `xcodebuild -list` reporting a `ShareComputeCore` scheme beside
`Infer Ring` and `Ring` (see `findings.md` F16). The earlier worry — that a package root which is an
*ancestor* of the `.xcodeproj` might be rejected — did not materialise, and the fallback of moving
the package into `Packages/ShareComputeCore` is not needed.

## What the core provides

- **`Epoch`** — membership is versioned rather than mutated, because MLX groups cannot be joined
  or left and every generated token is an all-ranks barrier.
- **`Lease`** — time-bounded permission to hold work, clamped to what each OS can honour. An iOS
  device gets 30s because `willResignActive` leaves only a brief window to drain.
- **`NodeState` / `NodeStateMachine`** — `activeCore`, `activeElastic`, `opportunistic`,
  `draining`, `suspended`, as a data table. `draining` is added to the spec's set: without it,
  "leaving" and "already gone" are indistinguishable, which is the ambiguity behind the hang.
- **`MembershipService`** — epochs, lease renewal, heartbeat hysteresis, anti-flap dwell. Performs
  no I/O and owns no timer: the host reports outcomes and calls `tick(at:)`, so failure detection
  is a pure function of injected time.
- **`StagePlanner`** — replaces `ModelManager.assignShardMetadata`, fixing three defects (see below).

### Shard planning defects fixed

1. **Remainder concentration.** Truncating division per device, with `endLayer = nLayers` on the
   last rank, put every device's rounding error on one node. On a 32/6/6 GB ring over 48 layers it
   produced `(34, 6, 8)` — overshooting by 22% on the node with the least headroom. Largest-remainder
   apportionment now bounds every node to the floor or ceiling of its exact entitlement.
2. **Unknown memory corrupting the plan.** A missing hardware profile was replaced with a 1 GB
   guess while that node was excluded from the capacity total, so it counted in the numerator but
   not the denominator; shard bounds could then exceed `nLayers` and be silently clamped to an
   empty range. `PlannedMember` requires a real profile, making the state unrepresentable.
3. **Aggregate-only feasibility.** `checkIfCanLoad` compared model size against the *sum* of ring
   memory, so a plan could pass and then OOM on the smallest device. Every node is now checked
   against its own assignment.

### One deliberate divergence from the spec

Spec §12.1 sets `can_host_required_stage = false` for iOS. Applied literally that deletes
infer-ring's headline feature — combining an iPhone's RAM with a Mac's is the product. iOS keeps
its eligibility here; the safety the spec is reaching for comes instead from short OS-clamped
leases and mandatory drain-on-background.

## Agent roster

`.claude/agents/` holds 20 role definitions — two architects, two specialists, five platform trios
(designer / developer / backend), and a tester. `CLAUDE.md` carries the project context every one of
them starts from; `.claude/skills/orchestration/SKILL.md` holds the routing, file-ownership and
escalation rules.

Nothing is spawned by default. Dispatch a role when the work needs knowledge you would otherwise
build from scratch, when two tasks touch disjoint paths, or when you want the work isolated in a
worktree — not merely because a task has several parts.

### Slash commands

Every role also has a command — `/mac-backend`, `/tester`, one per file in `.claude/agents/`, defined
under `.claude/skills/`. Typing one **is** the dispatch decision, so it skips the bar above.

These are a human surface, not a second router. All 20 carry `disable-model-invocation: true`, which
removes them from the model's listing entirely: they cost nothing per turn and cannot compete with
`orchestration` for routing. Two behaviours differ by kind:

- **The eleven active roles fork.** The agent definition becomes the subagent's system prompt, so the
  command carries dispatch mechanics only — arguments, the ownership pre-flight, the report contract
  — and never role knowledge. A 40-line body cap in the validator is what actually enforces that;
  pointing at the agent file would not.
- **The nine gated roles answer inline and spawn nothing.** Fork resolution falls back to
  `general-purpose` on an agent name it cannot resolve, and that fallback is **silent** — one typo in
  `agent:` produces no error anywhere. Inline is the option that fails *visibly*. The validator's
  `agent:`-resolution check is the highest-value line in it.

  Inline is **not** a tool restriction, and an earlier version of this section said otherwise. The
  skill body runs with the caller's toolset, and the gated agents' own toolsets never apply on this
  path at all — nor are those agents read-only: all nine carry `Bash`. The gate is **instructional**,
  enforced by the definitions refusing the work. (A role *forked* through the `Agent` tool does get
  its narrower toolset, so that path is a real if partial narrowing — but not the command path, and
  `Bash` writes regardless.) `findings.md` F19 records the correction.

Three workflow commands are model-invocable, deliberately — they are not routing decisions:
`/verify` (the verification matrix and, as importantly, what it does *not* cover), `/patches` (apply
order, the `finalize()` precondition, the negative control) and `/findings` (append-only, next
F-number, evidence with `file:line`, mandatory unverified section).

`/verify` knowingly shadows a same-named built-in skill. That is normally a reason never to create
one, and is the documented exception: how a project verifies is project-specific, and here it has to
name the large half that this container cannot check at all.

**That shadow is a design intent, not an observed behaviour.** It rests on reading the shipped CLI
bundle, where a project skill displaces a same-named bundled one; nothing here has confirmed it at
runtime. Typing `/verify` in an interactive session and seeing *this* file's content — the five
commands and the container's limits — rather than the generic built-in is the check that would
settle it, and it has not been run.

Three things worth knowing before dispatching:

- **Nine roles are gated.** `linux-*`, `windows-*` and `android-*` have no runtime to target: MLX is
  Apple-only, so until the specification's Phase 1a (portable IR + wire protocol) and a non-MLX
  execution path exist, those nodes cannot run. Their definitions say so and refuse the work.
- **One writer per path.** `Sources/ShareComputeCore/**` belongs to `senior-architect` alone;
  everyone else files a change request in `findings.md`. That boundary is what contained both MLX
  spike failures without touching the core.
- **Most platform work cannot be verified here.** This container has no macOS, Xcode, Android SDK or
  Windows. Every definition carries the verification matrix and the rule to state what was *not*
  verified.

```bash
python3 scripts/validate-agents.py    # 20 agents (9 gated), 24 skills (20 role, 3 workflow)
```

## Building

Requires Swift 6.0+. The core has no dependencies and builds on Linux, macOS and Windows.

```bash
swift build
swift test
```

The Xcode project under `Apps/InferRing/` requires macOS and Apple Silicon and is not built by SPM.
