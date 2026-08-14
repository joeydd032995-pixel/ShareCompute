import MLX
import XCTest

/// The runtime half of Stage 2, and the only part of it checkable without two Apple devices.
///
/// `Patches/mlx/tests/group_finalize_test.cpp` covers the same semantics but **mirrors** the patched
/// C++ rather than including it — MLX cannot be compiled on the Linux container the harness runs in.
/// A mirror can agree with itself while disagreeing with the real thing. This file is the first
/// thing to execute the actual path, across all three repositories at once:
///
///     Swift  DistributedGroup.deinit / .finalize()      mlx-swift/0001 + /0002
///     C      mlx_distributed_group_free / _finalize     mlx-c/0001
///     C++    mlx::core::distributed::finalize()         mlx/0002
///
/// **One test method, on purpose.** `distributed::init` memoises into a process-lifetime cache, so
/// every test in this process shares that state and XCTest does not guarantee method order. Split
/// into several methods these assertions would couple invisibly and fail in whichever order CI
/// happened to pick. Keeping it as one ordered sequence is what makes it deterministic.
final class DistributedGroupLifecycleTests: XCTestCase {

    func testFinalizeTracksHandleLifetimeAcrossTheSwiftCAndCxxBoundaries() {
        // Useful in the log when something here goes wrong, but deliberately not asserted on: a CI
        // runner has no MLX_HOSTFILE, so no real backend initialises and this is expected to be
        // false. The cache semantics under test do not depend on it.
        print("MLX distributed available: \(DistributedGroup.isAvailable)")

        // 1. Nothing has been initialised, so there is nothing to tear down. A `false` here would
        //    mean finalize() reports failure on an empty cache, which would make it useless as a
        //    precondition check. Valid as the first statement of the first method only — see the
        //    note above about why this file has exactly one.
        XCTAssertTrue(
            DistributedGroup.finalize(),
            "finalize() on an untouched cache must succeed rather than report a phantom handle")

        // 2. A live handle must block teardown.
        do {
            let group = DistributedGroup.initialize(strict: false)

            XCTAssertFalse(
                DistributedGroup.finalize(),
                "finalize() must refuse while a DistributedGroup is still held")

            // Load-bearing. ARC may release at last use, so without this the compiler would be free
            // to destroy `group` before the assertion above — which would then pass for a reason
            // that has nothing to do with the patch, and keep passing if the patch were reverted.
            withExtendedLifetime(group) {}
        }

        // 3. Once the last handle is gone, teardown succeeds.
        //
        //    This single assertion is also the ARC check. The C++ use count only drops when
        //    `deinit` calls `mlx_distributed_group_free`, so a `true` here proves the Swift
        //    destructor reached the C symbol and the C++ side observed it. A separate "did deinit
        //    fire" test would need an injection point that does not exist, and would prove less.
        XCTAssertTrue(
            DistributedGroup.finalize(),
            "finalize() must succeed once the last DistributedGroup has been released")
    }

    // MARK: - Deliberately absent
    //
    // There is no assertion that a re-`initialize(strict:)` after a successful `finalize()` returns
    // a *genuinely new* group — the property that pins the defect this whole milestone exists to
    // fix. It cannot be tested on one machine, and writing it here would be worse than omitting it.
    //
    // `ring::init` returns nullptr unless both MLX_HOSTFILE and MLX_RANK are set, so a CI runner
    // gets an `EmptyGroup`. The no-backend path in `distributed::init` caches that group under the
    // last-tried backend key and **never under "any"** — `cache.insert({"any", group})` sits in the
    // else branch. So a second `initialize()` misses the cache and builds a fresh group whether or
    // not `finalize()` ever ran. The assertion would pass identically against unpatched MLX.
    //
    // A one-rank ring is not a way around it either: `connect_to = (rank_ + 1) % size_` is 0 when
    // size is 1, so rank 0's peer is itself and it connects before it listens. It throws after
    // ~5 s (CONN_ATTEMPTS 5, CONN_WAIT 1000 ms).
    //
    // A real single-host test needs two processes on 127.0.0.1 at different ports with MLX_RANK 0
    // and 1. Until that exists the property stays on the two-device hardware list. See findings.md
    // F23 before adding it back.
}
