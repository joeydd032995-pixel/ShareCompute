---
name: patches
description: Work with ShareCompute's MLX patch set — the five patches across mlx, mlx-c and mlx-swift that let a departing rank fail instead of hang and let the distributed group be torn down and rebuilt.
argument-hint: <optional — a patch to check or extend, otherwise reports the set's state>
---

# The MLX patch set

`Patches/README.md` is the reference; this is the working procedure. Read that file for the pinned
revisions and the full evidence — the numbers below are load-bearing and must not be re-derived from
memory.

## $ARGUMENTS

If empty, report the state of the five patches, whether each still applies to its pin, and what
Stage 3 still needs. If it names a patch, work on that one and keep the constraints below.

## What the set is for

| Repo | Patch | Effect |
|---|---|---|
| `mlx` | `0001-ring-fail-instead-of-hang` | a departing peer surfaces as a thrown error within ~1 token instead of wedging every survivor forever |
| `mlx` | `0002-distributed-finalize` | `finalize()` — the teardown `distributed::init`'s function-local static cache never had |
| `mlx-c` | `0001-export-group-free-and-finalize` | the `extern "C"` exports for both |
| `mlx-swift` | `0001-free-group-and-expose-finalize` | restores the commented-out `deinit` free; adds `static func finalize() -> Bool` |

## Three constraints that make the obvious change wrong

1. **Apply order is not cosmetic.** `mlx` first. The mlx-c patch calls
   `mlx::core::distributed::finalize()`, which does not exist until `mlx/0002` declares it —
   applying mlx-c first gives `error: 'finalize' is not a member of 'mlx::core::distributed'`.

2. **`finalize()` returns `false` and changes nothing while any group handle is still held.** In
   infer-ring that means the loaded `ModelContext` — whose sharded layers each retain the group,
   `findings.md` F14 — **and** `MLXManager.group`, not just one of them. The order is: release every
   handle → `finalize()` → re-`init()` **only on `true`**. Treating a `false` as success re-forms the
   ring with the departed member still in it, which is worse than the hang because it looks fine.

3. **An exception must never escape a dispatched MLX task.** `CommandEncoder::dispatch` hands the
   callable to `scheduler::enqueue` with no `try`/`catch` anywhere in that path, so an escaping
   exception unwinds a scheduler thread with no handler and calls `std::terminate()` — a crash in a
   shipping App Store app. Failures are *recorded* and rethrown later from the caller's own thread.
   Relatedly, `std::future::wait()` silently discards an exception; every consumption site must use
   `get()`. Setting exceptions on promises without converting all ten sites in `ring.cpp` produces
   silent data corruption instead of a hang, which is worse, because a hang is at least visible.

## Checking the set

Three layers, all runnable here. The full command block, including the sibling-clone setup, is in
`Patches/README.md` under *Applying* and *Verification*; run it from a directory holding the three
clones.

- **Compile** every patched translation unit with `g++ -fsyntax-only -std=c++17`, using mlx-swift's
  vendored `json` and `fmt` headers.
- **The negative control**, which matters more than the green compile: build mlx-c against the
  *unpatched* MLX header and confirm it **fails**. Without it, a passing compile is equally
  consistent with two changes that merely look consistent.
- **The runtime harnesses** — 47 checks, 16 for Stage 1 and 31 for Stage 2, plus the Stage 2 case
  under ThreadSanitizer. `/verify` runs these.

If you change a patched file, change its harness in the same commit. The harnesses mirror the patched
code rather than including it, so they will keep passing against a design that no longer exists.

## What is still missing

Stage 2 supplies the teardown; it does **not** re-form the ring. That is Stage 3, in the app, and it
is not started.

## Report

**State what you verified and what you did not**, and keep the line where it actually sits now.

**CI-backed:** all five patches build as part of MLX under a real Apple toolchain, for
`arm64-apple-macos` and iOS Simulator; `mlx_distributed_group_free` and `mlx_distributed_finalize`
link across all three repositories; `DistributedGroup.swift` type-checks with `import Cmlx` genuinely
resolved. Do not hedge these — they are checked on every run.

**Still unverified, and it is the whole of what matters behaviourally:** none of these patches has
ever been *run*. No ring has formed, `finalize()` has never been called, `~RingGroup()` and
`~SocketThread()` have never executed against a departed peer, and the fail-instead-of-hang path has
never fired on a real socket. Say so every time. It needs two Apple devices.
