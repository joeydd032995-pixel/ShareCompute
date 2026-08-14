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

## Not verified

The ring itself. This forms no ring, sends no collective, and exercises neither the Stage 1
fail-instead-of-hang path nor `~RingGroup()` against a departed peer. Those need two Apple devices.
