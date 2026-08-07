import Foundation

public struct LeaseID: Hashable, Codable, Sendable, CustomStringConvertible {
    public let rawValue: String

    public init(_ rawValue: String) {
        self.rawValue = rawValue
    }

    public init() {
        self.rawValue = UUID().uuidString
    }

    public var description: String { rawValue }
}

/// A time-bounded grant permitting a node to hold work in a particular epoch. Spec §10.1.
///
/// The lease is the mechanism that replaces the current code's implicit assumption that every
/// peer stays reachable forever. A node holds work only while its lease is valid; the scheduler
/// refuses to place a stage that would outlive the lease.
public struct Lease: Codable, Sendable, Equatable {
    public let id: LeaseID
    public let nodeID: NodeID
    public let epoch: Epoch
    public let issuedAt: Date
    public let expiresAt: Date
    public let allowedStageTypes: Set<StageType>

    /// 0...1. Fraction of recent heartbeats answered on time. Feeds placement preference —
    /// a node that keeps flapping gets less important work even while nominally alive.
    public let stabilityScore: Double

    public init(
        id: LeaseID = LeaseID(),
        nodeID: NodeID,
        epoch: Epoch,
        issuedAt: Date,
        expiresAt: Date,
        allowedStageTypes: Set<StageType>,
        stabilityScore: Double = 1.0
    ) {
        self.id = id
        self.nodeID = nodeID
        self.epoch = epoch
        self.issuedAt = issuedAt
        self.expiresAt = expiresAt
        self.allowedStageTypes = allowedStageTypes
        self.stabilityScore = stabilityScore
    }

    public func remainingTime(at now: Date) -> TimeInterval {
        max(0, expiresAt.timeIntervalSince(now))
    }

    public func isValid(at now: Date) -> Bool {
        now < expiresAt
    }

    /// Spec §9.1: place a stage only when it is expected to finish inside the current lease.
    ///
    /// Deliberately strict — `>=` rather than `>` would let a stage finish exactly as the lease
    /// lapses, which is the race this whole mechanism exists to avoid.
    public func canHost(
        _ stage: StageType,
        estimatedDuration: TimeInterval,
        at now: Date
    ) -> Bool {
        guard isValid(at: now) else { return false }
        guard allowedStageTypes.contains(stage) else { return false }
        return estimatedDuration < remainingTime(at: now)
    }

    public func renewed(until newExpiry: Date, stabilityScore: Double) -> Lease {
        Lease(
            id: id,
            nodeID: nodeID,
            epoch: epoch,
            issuedAt: issuedAt,
            expiresAt: newExpiry,
            allowedStageTypes: allowedStageTypes,
            stabilityScore: stabilityScore
        )
    }
}
