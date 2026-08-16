# Swift lifecycle check

The runtime half of Stage 2, and the only part of it that can be checked without two Apple devices.

`swift test` from this directory, on macOS. CI runs it as
[`mlx-lifecycle.yml`](../../../.github/workflows/mlx-lifecycle.yml).

## What it is for

The two Xcode jobs prove the patched MLX **compiles and links**. Nothing proved that any of it
**runs**. This does, for one property:

> ARC releasing a `DistributedGroup` is observed by the C++ use count that `finalize()` checks.

That single assertion crosses all three patched repositories in one go — Swift `deinit` →
`mlx_distributed_group_free` → `mlx::core::distributed::finalize()` — and it is the precondition
every caller of `finalize()` depends on. `MLXManager.teardown()` is built on it: it releases the
`ModelContext` and `MLXManager.group`, then treats a `false` as an error. If the C++ side did not
actually see ARC's release, `teardown()` would report failure forever and re-formation would never
happen.

## Why it is not `Patches/mlx/tests/group_finalize_test.cpp`

That harness covers the same semantics, but it **mirrors** the patched C++ rather than including it,
because MLX cannot be compiled on the Linux container it runs in. A mirror can agree with itself
while having drifted from the real thing. This is the first check to execute the actual code.

Both are kept. The C++ harness is seconds rather than fifteen minutes, runs on Linux, and covers
cases this cannot — the concurrency case under ThreadSanitizer, and a case reproducing the
*unpatched* behaviour.

## Why it is a standalone package

Two independent reasons, either one sufficient:

- The root `Package.swift` declares **zero dependencies**, and `CLAUDE.md` forbids adding one. That
  boundary is why `ShareComputeCore` builds and tests on Linux.
- The Xcode project has **no test target** — only an application and a framework — so adding one
  means creating a target, build phases and a scheme entry inside `project.pbxproj`, which is
  generated and which a bad edit leaves unopenable.

SwiftPM resolves the root package's targets only under `Sources/` and `Tests/`, so this package is
invisible to `swift test` at the repository root.

It depends on the fork by **branch**, not commit, matching what `project.pbxproj` names — so a
regression pushed to the fork fails here rather than going unnoticed until someone rebuilds the app.

## What it deliberately does not assert

That a re-`initialize(strict:)` after a successful `finalize()` returns a **genuinely new** group.
That is the property pinning the defect this milestone exists to fix, and it **cannot be tested on
one machine** — see `findings.md` F23. On a runner with no `MLX_HOSTFILE`, the no-backend path
caches its `EmptyGroup` under the last-tried backend key and never under `"any"`, so a second
`initialize()` returns a fresh group whether or not `finalize()` ran. The assertion would pass
identically against **unpatched** MLX.

A one-rank ring does not help: rank 0's peer is itself and it connects before it listens, throwing
after about five seconds. A real single-host test needs two processes on `127.0.0.1` at different
ports. Until then the property stays on the two-device hardware list.

The reasoning is repeated in a comment in the test file, because that is where someone will be
standing when they think of adding it.

**That "until then" is now being tested — see below.** The condition this section named has been
built rather than waited for.

## `ring-formation.sh` — two processes, one machine, a real ring

`bash ring-formation.sh` on macOS. CI runs it as
[`mlx-ring.yml`](../../../.github/workflows/mlx-ring.yml).

This section of the README already identified the missing piece: *"a real single-host test needs two
processes on `127.0.0.1` at different ports."* `RingFormationProbe` is one such process and
`ring-formation.sh` launches one per rank, so the answer stops being deferred.

The reading behind it, from `mlx/distributed/ring/ring.cpp`:

- `initialize(strict:)` → `mlx_distributed_init(strict, nil)` → `distributed::init(strict, "any")`.
- `"any"` on Apple finds nccl unavailable and falls through to `ring::init(false)`.
- `ring::init` needs exactly `MLX_HOSTFILE` and `MLX_RANK` (`ring.cpp:944`). The hostfile is a JSON
  array of `"ip:port"`, one entry per rank; rank N listens on its own address and connects to rank
  (N+1) % size (`ring.cpp:441-454`), in an order that avoids deadlock.

Nothing there is device-specific, which is why **`CLAUDE.md`'s "needs two or more real devices" may
be conflating two *ranks* with two *machines*.** That is still a reading, and reading has been wrong
three times here (F18, F20, F22), each time in the reassuring direction — so the job decides it, not
the argument.

The probe asserts three things in order, and each is a first for this project if it passes:

1. **A group of the requested size and rank forms.** Size 1 means `ring::init` declined and an
   `EmptyGroup` was built — a clean, distinguishable negative rather than a crash, which is why the
   probe uses `strict: false`.
2. **`allGather` returns every rank's contribution.** A group that constructs but cannot exchange
   data is not a ring. This is also the exact operation the product depends on — `PipelineLastLayer`
   runs it on every forward pass, so every generated token is an all-ranks barrier (load-bearing
   fact #2).
3. **`finalize()` refuses while a handle is held, then succeeds once released** — against a *real*
   ring rather than the `EmptyGroup` the single-process test can reach. F23 is exactly why this
   matters: the assertion above is vacuous without a hostfile, and meaningful with one.

Exit codes are the interface, so a failure says which of the three it was, and a watchdog turns a
ring that never closes into a reported `HANG` rather than a job that runs to its limit.

### The verdict is three-way, and that is not a detail

| outcome | meaning |
|---|---|
| every rank `0` | the ring formed |
| any rank `10`–`13`, or `124` (hang) | the ranks ran and the ring did not form — **a result** |
| any rank `14`, `127`, anything else | **INCONCLUSIVE — draw no conclusion** |

The first version had only two outcomes, and so reported `timeout: command not found` (exit 127) as
*"no loopback ring, CLAUDE.md's claim stands as written"*. Nothing had been tested. A harness that
cannot say "I could not run" will eventually confirm whatever you already believed — see F29.

`124` is deliberately a *result*: a rank that started, tried to reach its peer and blocked has said
something real, and it is the exact failure this project exists to kill.

### Testing the launcher without MLX

The reason it shipped broken is that it could only be exercised by a macOS CI run — it builds MLX
before doing anything. `PROBE_BIN` skips the build so the launcher's own logic runs against a stub:

```bash
printf '#!/usr/bin/env bash\nexit 10\n' > /tmp/stub && chmod +x /tmp/stub
PROBE_BIN=/tmp/stub TIMEOUT=4 bash ring-formation.sh
```

Verified this way on Linux across exit 0, exit 10, a missing command, and a process that blocks
forever. Do that before changing the launcher; it takes seconds and needs no Apple hardware.

**macOS ships no `timeout(1)`** — it is GNU coreutils. The bound here is a plain-bash watchdog for
that reason. The `Spikes/llamacpp-rpc/` harnesses still use `timeout` correctly, because they run
on Linux only.

The CI job carries `continue-on-error: true` **deliberately and temporarily**. Until the experiment
has returned once, red would mean "the hypothesis was wrong", not "the branch is broken". Remove it
once the answer is recorded — either way it then becomes a real regression gate.

## Not verified

The ring itself. This forms no ring, sends no collective, and exercises neither the Stage 1
fail-instead-of-hang path nor `~RingGroup()` against a departed peer. Those need two Apple devices.
