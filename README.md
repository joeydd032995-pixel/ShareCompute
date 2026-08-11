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
| `Tests/ShareComputeCoreTests/` | 58 tests, no network and no sleeping. |
| `Apps/InferRing/` | The vendored infer-ring app, unmodified. The first adapter. |
| `Patches/` | Patches to MLX, mlx-c and mlx-swift. Start at [`Patches/README.md`](Patches/README.md). |
| `findings.md` | Research log, including both spike results. Read this first. |
| `task_plan.md` | Phase status and decisions. |
| `progress.md` | Session log and test results. |

## Status

**Detection ships. The MLX patches that make re-formation possible are written; wiring them into
the app is not done.**

| Milestone | State |
|---|---|
| M1 — detection without re-formation | merged |
| M2 Stage 1 — a departing peer fails instead of hanging | patch written, 16 harness checks |
| M2 Stage 2 — the group can be torn down and rebuilt | patches written, 27 harness checks |
| M2 Stage 3 — epoch re-formation in the app | **not started** |

None of the patches has been built as part of MLX or run on Apple hardware. See
[`Patches/README.md`](Patches/README.md).

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

### What is implemented: detection without re-formation

The ring cannot be rebuilt, so the achievable goal is to **turn an indefinite silent hang into a
reported failure**. That is what ships here:

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

The wedged MLX thread stays wedged until the process exits — nothing here changes that, and the UI
says so rather than offering a reconnect button that could not work.

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

**If Xcode rejects the package reference**, the likely cause is that the package root is an
*ancestor* of the `.xcodeproj` rather than a sibling. This could not be verified here — no macOS.
The mechanical fix is to move the package into its own directory and repoint the reference:

```bash
mkdir -p Packages/ShareComputeCore
git mv Package.swift Sources Tests Packages/ShareComputeCore/
# then in project.pbxproj: relativePath = "../../Packages/ShareComputeCore"
```

Note that this also moves the package out of the repository root, so the layout table above and the
`swift build` / `swift test` commands below then apply from `Packages/ShareComputeCore` rather than
from the root.

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
python3 scripts/validate-agents.py    # lint the roster
```

## Building

Requires Swift 6.0+. The core has no dependencies and builds on Linux, macOS and Windows.

```bash
swift build
swift test
```

The Xcode project under `Apps/InferRing/` requires macOS and Apple Silicon and is not built by SPM.
