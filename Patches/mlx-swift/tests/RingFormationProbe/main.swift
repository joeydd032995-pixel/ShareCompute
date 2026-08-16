// One rank of a loopback MLX ring.
//
// WHY THIS EXISTS
//
// `CLAUDE.md` has said since the first session that a multi-device ring "needs two or more real
// devices", and every milestone has been reported as unrun on that basis. Reading
// `mlx/distributed/ring/ring.cpp` suggests that is wrong -- or at least conflates *two ranks* with
// *two machines*:
//
//   - `DistributedGroup.initialize(strict:)` calls `mlx_distributed_init(strict, nil)`, which
//     reaches `distributed::init(strict, "any")`.
//   - On Apple, `"any"` finds nccl unavailable and falls through to `ring::init(false)`.
//   - `ring::init` needs exactly two things: `MLX_HOSTFILE` and `MLX_RANK` (ring.cpp:944). The
//     hostfile is a JSON array of `"ip:port"`, one entry per rank, and rank N listens on its own
//     address and connects to rank (N+1) % size (ring.cpp:441-454).
//
// Nothing on that path is device-specific. Two processes on one machine with different loopback
// ports should therefore form a real ring -- which would make the whole "unrun" half of Milestone 2
// testable on a free CI runner rather than on hardware nobody here has.
//
// That is a reading, and reading has been wrong three times on this project (F18, F20, F22), each
// time in the reassuring direction. This probe is how it gets settled. A negative result is worth
// as much as a positive one, so the failures are enumerated and distinguishable rather than
// collapsing into "non-zero".
//
// Driven by `ring-formation.sh`, which writes the hostfile and launches one of these per rank.

import Foundation
import MLX

// Exit codes are this program's interface -- the launcher decodes them, so each means one thing.
let exitOK: Int32 = 0
let exitNoRing: Int32 = 10 // ring::init declined; an EmptyGroup was built
let exitWrongShape: Int32 = 11 // a group formed, but not the one asked for
let exitCollectiveFailed: Int32 = 12 // allGather returned the wrong data
let exitFinalizeWrong: Int32 = 13 // teardown did not behave as Stage 2 requires
let exitBadEnvironment: Int32 = 14 // the launcher set this up wrong; not a finding

let environment = ProcessInfo.processInfo.environment

guard let rankText = environment["MLX_RANK"], let expectedRank = Int32(rankText),
    let sizeText = environment["PROBE_EXPECT_SIZE"], let expectedSize = Int32(sizeText),
    expectedSize > 0
else {
    FileHandle.standardError.write(
        Data("MLX_RANK and PROBE_EXPECT_SIZE must both be set to integers\n".utf8))
    exit(exitBadEnvironment)
}

func log(_ message: String) {
    print("[rank \(expectedRank)] \(message)")
    fflush(stdout)
}

func fail(_ code: Int32, _ message: String) -> Never {
    log("FAIL: \(message)")
    exit(code)
}

// Force the CPU device before touching any MLXArray. A GitHub Actions macOS runner is headless and
// MLX cannot load its default metallib there: the run that first reached the collective died with
// "Failed to load the default metallib ... at mlx/c/array.cpp:232", *after* the ring had already
// formed. Nothing in this probe needs the GPU -- the ring backend's collectives run on a CPU stream
// anyway, and the payload is one Int32 per rank.
Device.setDefault(device: Device(.cpu))

log("MLX_HOSTFILE = \(environment["MLX_HOSTFILE"] ?? "<unset>")")

// Reported for the record, not used as a gate. `ring::is_available()` returns true unconditionally
// -- it is a compile-time capability check, so on any Apple build this is the constant `true`. A
// previous version of the lifecycle test predicted `false` here and was wrong (see F23's amendment).
log("DistributedGroup.isAvailable = \(DistributedGroup.isAvailable)")

do {
    // strict: false deliberately. In strict mode a missing hostfile throws from C++ and comes back
    // through mlx-c as an error; non-strict returns an EmptyGroup of size 1, which is a *clean and
    // detectable* negative. That is what makes "the ring did not form" a distinct, legible outcome
    // rather than a crash.
    let group = DistributedGroup.initialize(strict: false)

    log("group formed: size=\(group.size) rank=\(group.rank)")

    if group.size == 1 && expectedSize > 1 {
        fail(
            exitNoRing,
            """
            no ring -- size 1 means ring::init declined and distributed::init fell back to \
            EmptyGroup. Either MLX_HOSTFILE/MLX_RANK did not reach this process, or the hostfile \
            did not parse. This is the result that would confirm CLAUDE.md's "needs two or more \
            real devices" as literally true.
            """)
    }

    guard group.size == expectedSize, group.rank == expectedRank else {
        fail(
            exitWrongShape,
            "expected size \(expectedSize) rank \(expectedRank), got size \(group.size) "
                + "rank \(group.rank)")
    }

    // The collective is the point. A group that constructs but cannot exchange data is not a ring,
    // and `allGather` specifically is the operation the product depends on: `PipelineLastLayer`
    // runs it on every forward pass, so every generated token is an all-ranks barrier
    // (CLAUDE.md load-bearing fact #2). If this returns, ranks genuinely reached each other.
    //
    // 100 + rank rather than the rank itself, so a result of all-zeros -- the most likely shape of
    // a silent failure -- cannot be mistaken for rank 0's correct contribution.
    let contributionValues: [Int32] = [100 + expectedRank]
    let contribution = MLXArray(contributionValues)
    let gathered = group.allGather(contribution)

    // `asArray` evaluates internally, so the lazy graph is forced here and any collective failure
    // surfaces at this line rather than silently later.
    let gatheredValues = gathered.asArray(Int32.self)
    let expectedValues = (0 ..< expectedSize).map { Int32(100 + $0) }

    log("allGather -> \(gatheredValues)")

    guard gatheredValues == expectedValues else {
        fail(
            exitCollectiveFailed,
            "allGather returned \(gatheredValues), expected \(expectedValues)")
    }

    // Stage 2's contract, now against a *real* ring rather than the EmptyGroup the single-process
    // lifecycle test could only reach. F23 recorded that on one machine the re-init assertion was
    // vacuous because ring::init always declined; with a hostfile it no longer declines, so this
    // is the first time these two lines mean anything.
    guard DistributedGroup.finalize() == false else {
        fail(exitFinalizeWrong, "finalize() reported success while a live handle was still held")
    }

    // Without this, ARC may release `group` at its last use above, and the assertion just made
    // would be testing nothing.
    withExtendedLifetime(group) {}
}

// Last handle gone, so teardown must now succeed. On a real ring this actually closes sockets and
// destroys the RingGroup -- the operation that has never once executed in this project.
guard DistributedGroup.finalize() else {
    fail(exitFinalizeWrong, "finalize() refused after the last handle was released")
}

log("OK -- ring formed, allGather correct, finalize refused-then-succeeded")
exit(exitOK)
