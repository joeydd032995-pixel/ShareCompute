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
        }
    }
}
