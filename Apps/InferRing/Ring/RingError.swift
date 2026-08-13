//
import Foundation

public enum RingError: LocalizedError {
    case failed(String)
    /// `DistributedGroup.finalize()` returned `false`: at least one group handle was still held, so
    /// the teardown did nothing and the cached group is still live.
    ///
    /// Separate from `.failed` because the *response* differs. This is never retryable as-is —
    /// re-initialising now returns the same stale group, and re-calling `finalize()` without
    /// releasing the surviving handle fails identically. Something must be released first.
    case handlesOutlivedTeardown
    /// This build's MLX has no `DistributedGroup.finalize()`, so the group cannot be torn down at
    /// all — the ring degrades to Milestone 1 behaviour: loss is detected and reported, and a
    /// restart is genuinely the only recovery.
    ///
    /// Deliberately separate from `handlesOutlivedTeardown`. That one means a handle survived and
    /// the fix is to find it; this one means the operation is absent and the fix is to build
    /// against the patched fork. Same symptom, unrelated remedies.
    case finalizeUnavailable

    public var errorDescription: String? {
        switch self {
        case .failed(let message):
            return "Ring Error: \(message)"
        case .handlesOutlivedTeardown:
            return """
                Ring Error: the distributed group could not be torn down because a handle to it is \
                still held. The usual cause is a loaded model — its sharded layers each retain the \
                group — so the ModelContext must be released before teardown.
                """
        case .finalizeUnavailable:
            return """
                Ring Error: this build cannot rebuild the ring. DistributedGroup.finalize() is \
                supplied by Patches/mlx-swift/0001, which is not in the MLX this app was compiled \
                against; build with MLX_HAS_FINALIZE against the patched fork to enable it.
                """
        }
    }
}
