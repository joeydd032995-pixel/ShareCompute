# Progress log

## Session 1 — 2026-08-07

### Environment
- Container is **x86_64 Linux (Ubuntu 24.04)**. No macOS, no Xcode, no Apple Silicon.
- No Swift toolchain present at start → installed **Swift 6.1.2** to `/opt/swift`.
- Consequence: `ShareComputeCore` is fully buildable and testable here; the Xcode project and
  anything importing MLX or UIKit is not.

### Done
1. Vendored `infer-ring-main` → `Apps/InferRing/` (unmodified).
2. SPM workspace with a dependency-free `ShareComputeCore` target.
3. Planning files per `planning-with-files`.
4. `CapabilityProfile`, `Lease`, `Epoch`, `NodeState` + `NodeStateMachine`, `RingClock`.
5. `StagePlanner` replacing `ModelManager.assignShardMetadata`.
6. `MembershipService` — epochs, leases, heartbeat hysteresis, anti-flap dwell.
7. **Spikes A and B — both answered from source, both failed.** See `findings.md`.

### Test results

`/opt/swift/usr/bin/swift test` — **46 tests, 0 failures.**

| Suite | Tests |
|---|---|
| `NodeStateMachineTests` | 14 |
| `StagePlannerTests` | 17 |
| `MembershipServiceTests` | 15 |

Includes two 500-iteration property tests over apportionment (exact coverage; no node exceeding
the ceiling of its proportional entitlement).

### Notable events

**A test caught a design flaw in my own algorithm.** The first `apportion` implementation granted
every node one layer up front and divided the remainder by weight. On a 30 GB / 6 GB ring over 36
layers that yielded 29/7 instead of 30/6 — systematically pushing layers *toward* the
memory-constrained device, a milder form of the very defect being fixed. Corrected to apportion
the full total by weight first, applying the minimum-one-layer rule afterwards by taking from the
largest holder.

**Both spikes failed, and the plan's gate fired.** Rather than run them on hardware I do not have,
I read the pinned sources: the `N1k1tung/mlx-swift` fork at `ios-distrib-0.3.0` and upstream
`ml-explore/mlx` at the pinned submodule commit `38ad257`. The answers were unambiguous at source
level — no group-free API and a process-lifetime static cache in `distributed::init` (Spike A);
abandoned promises with no exception on the ring backend's abort path (Spike B).

Phase 5 (adapter wiring) was **not** started as a result. The approved plan gated on Spike A and
said to stop and re-plan if it failed.

### Errors encountered

| Error | Attempt | Resolution |
|---|---|---|
| `swift: command not found` | 1 | Installed Swift 6.1.2 for Ubuntu 24.04 |
| SPM: "target has overlapping sources" | 1 | Test target directory did not exist yet; created `Tests/ShareComputeCoreTests/` |
| `XCTAssertEqual(_:_:accuracy:)` rejects optionals | 1 | Made the tests `throws` and used `try XCTUnwrap` |
| Apportionment biased toward the smallest node | 1 | Reordered: apportion by weight first, apply the min-one-layer floor afterwards |
| `git sparse-checkout init -q` — unknown switch | 1 | Dropped `-q`; the subcommand does not accept it |
| MLX not in `Package.resolved` | 1 | Xcode stores branch-pinned remote packages in `project.pbxproj`; found the forks there |

### Next

Blocked on a direction decision — see the three options at the end of `findings.md`. Nothing
further should be built against the assumption that an MLX group can be re-initialised.
