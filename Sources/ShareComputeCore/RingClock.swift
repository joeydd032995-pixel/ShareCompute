import Foundation

/// Source of wall-clock time.
///
/// Leases, heartbeat deadlines and epoch dwell times are all time-driven, so time is injected
/// rather than read from `Date()` directly. Tests advance a `TestClock` explicitly; nothing in
/// the suite has to sleep, which keeps failure detection tests fast and deterministic.
public protocol RingClock: Sendable {
    var now: Date { get }
}

public struct SystemClock: RingClock {
    public init() {}
    public var now: Date { Date() }
}
