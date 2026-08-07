import Foundation

public struct MembershipConfig: Sendable, Equatable {
    /// Requested lease length. Clamped per node by `maximumSupportableLeaseDuration`, so an iOS
    /// device never receives a lease longer than its OS will actually honour.
    public var leaseDuration: TimeInterval
    /// How often the adapter is expected to probe each peer.
    public var heartbeatInterval: TimeInterval
    /// Consecutive misses before eviction. Hysteresis: one dropped packet on Wi-Fi must not
    /// re-plan the ring.
    public var missedHeartbeatsBeforeEviction: Int
    /// Minimum time between epoch changes, so a flapping node cannot force continuous re-planning
    /// — every epoch change costs a model reload.
    public var minimumEpochDwellTime: TimeInterval

    public init(
        leaseDuration: TimeInterval = 60,
        heartbeatInterval: TimeInterval = 2,
        missedHeartbeatsBeforeEviction: Int = 3,
        minimumEpochDwellTime: TimeInterval = 5
    ) {
        self.leaseDuration = leaseDuration
        self.heartbeatInterval = heartbeatInterval
        self.missedHeartbeatsBeforeEviction = missedHeartbeatsBeforeEviction
        self.minimumEpochDwellTime = minimumEpochDwellTime
    }

    public static let `default` = MembershipConfig()
}

public enum EvictionReason: String, Sendable, Equatable {
    case heartbeatLost
    case leaseExpired
    case drained
}

public enum MembershipEvent: Sendable, Equatable {
    case nodeJoined(NodeID)
    case nodeDraining(NodeID)
    case nodeEvicted(NodeID, reason: EvictionReason)
    /// The planning set changed and a new generation has begun. The adapter must tear down the
    /// MLX group, re-plan shards, and re-initialise.
    case epochChanged(Epoch, members: [NodeID])
    /// Membership changed but the previous epoch is too young to replace. Anti-flap.
    case epochChangeDeferred(untilDwellElapsed: TimeInterval)
}

public struct MemberRecord: Sendable, Equatable {
    public var profile: CapabilityProfile
    public var state: NodeState
    public var lease: Lease?
    public var consecutiveMissedHeartbeats: Int
    public var lastHeardFrom: Date
    /// Exponentially weighted share of recent heartbeats answered. Feeds placement preference.
    public var stabilityScore: Double
}

/// Owns ring membership: who is in, what state they are in, whether their lease still holds, and
/// when those facts have changed enough to require a new epoch.
///
/// **Deliberately performs no I/O and owns no timer.** The host adapter probes peers and reports
/// outcomes via `heartbeatSucceeded`/`heartbeatFailed`, then calls `tick(at:)`. Every decision is
/// therefore a pure function of injected time, which is what lets failure detection — the thing
/// this milestone exists to add — be tested exhaustively without sleeping or opening a socket.
///
/// Not `Sendable`: the adapter confines it to a single actor.
public final class MembershipService {
    public let localNodeID: NodeID
    public let config: MembershipConfig

    private let clock: RingClock
    private var storage: [NodeID: MemberRecord] = [:]
    private var currentEpoch: Epoch
    private var lastEpochChangedAt: Date
    private var lastEpochMembers: Set<NodeID> = []

    private static let stabilitySmoothing = 0.3

    public init(
        localNodeID: NodeID,
        clock: RingClock,
        config: MembershipConfig = .default,
        epoch: Epoch = .initial
    ) {
        self.localNodeID = localNodeID
        self.clock = clock
        self.config = config
        self.currentEpoch = epoch
        self.lastEpochChangedAt = clock.now
    }

    // MARK: - Observation

    public var epoch: Epoch { currentEpoch }

    public var members: [NodeID: MemberRecord] { storage }

    public func record(for nodeID: NodeID) -> MemberRecord? { storage[nodeID] }

    /// Nodes that belong in the next shard plan.
    public var planningMembers: [PlannedMember] {
        storage.values
            .filter { $0.state.participatesInPlanning }
            .map { PlannedMember(profile: $0.profile, state: $0.state, lease: $0.lease) }
            .sorted { $0.nodeID < $1.nodeID }
    }

    // MARK: - Input

    /// A node has been discovered, or has re-published its capabilities.
    @discardableResult
    public func nodeAppeared(profile: CapabilityProfile) -> [MembershipEvent] {
        let now = clock.now

        if var existing = storage[profile.nodeID] {
            // Refreshed profile from a node already present: adopt the new facts (thermal state
            // and battery move constantly) but do not resurrect a departing node here — it must
            // complete its drain and rejoin explicitly.
            existing.profile = profile
            existing.lastHeardFrom = now
            existing.consecutiveMissedHeartbeats = 0
            storage[profile.nodeID] = existing
            return []
        }

        let state = NodeStateMachine.initialState(for: profile)
        storage[profile.nodeID] = MemberRecord(
            profile: profile,
            state: state,
            lease: issueLease(for: profile, state: state, at: now, stabilityScore: 1.0),
            consecutiveMissedHeartbeats: 0,
            lastHeardFrom: now,
            stabilityScore: 1.0
        )
        return [.nodeJoined(profile.nodeID)]
    }

    public func heartbeatSucceeded(from nodeID: NodeID) {
        guard var record = storage[nodeID] else { return }
        let now = clock.now
        record.consecutiveMissedHeartbeats = 0
        record.lastHeardFrom = now
        record.stabilityScore = Self.smoothed(record.stabilityScore, sample: 1.0)
        // Renewing on success is what keeps a healthy node's lease ahead of the scheduler.
        if record.state.participatesInPlanning {
            record.lease = issueLease(
                for: record.profile,
                state: record.state,
                at: now,
                stabilityScore: record.stabilityScore
            )
        }
        storage[nodeID] = record
    }

    public func heartbeatFailed(for nodeID: NodeID) {
        guard var record = storage[nodeID] else { return }
        record.consecutiveMissedHeartbeats += 1
        record.stabilityScore = Self.smoothed(record.stabilityScore, sample: 0.0)
        storage[nodeID] = record
    }

    /// A node told us it is going away — iOS `willResignActive`, a foreground service stopping, or
    /// a user-initiated stop. This is the path that turns the current deadlock into a planned
    /// departure, so it is handled eagerly rather than waiting for heartbeats to lapse.
    @discardableResult
    public func nodeAnnouncedDrain(_ nodeID: NodeID) -> [MembershipEvent] {
        guard var record = storage[nodeID] else { return [] }
        guard NodeStateMachine.apply(.enteredBackground, to: &record.state) else { return [] }
        record.lastHeardFrom = clock.now
        storage[nodeID] = record
        return [.nodeDraining(nodeID)]
    }

    /// Applies an arbitrary lifecycle event, e.g. `.powerDuress` from a thermal reading.
    @discardableResult
    public func apply(_ event: NodeEvent, to nodeID: NodeID) -> Bool {
        guard var record = storage[nodeID] else { return false }
        let moved = NodeStateMachine.apply(event, to: &record.state)
        if moved { storage[nodeID] = record }
        return moved
    }

    // MARK: - The tick

    /// Advances membership to `now`: expires leases, evicts unreachable nodes, completes drains,
    /// and starts a new epoch if the planning set has changed and the current epoch is old enough
    /// to replace.
    @discardableResult
    public func tick(at now: Date) -> [MembershipEvent] {
        var events: [MembershipEvent] = []

        for nodeID in storage.keys.sorted() {
            guard var record = storage[nodeID] else { continue }

            // A draining node leaves as soon as it has been given the chance to hand work back.
            if record.state == .draining {
                NodeStateMachine.apply(.drainCompleted, to: &record.state)
                storage[nodeID] = record
                events.append(.nodeEvicted(nodeID, reason: .drained))
                continue
            }

            guard record.state.participatesInPlanning else { continue }

            if record.consecutiveMissedHeartbeats >= config.missedHeartbeatsBeforeEviction {
                NodeStateMachine.apply(.heartbeatLost, to: &record.state)
                storage[nodeID] = record
                events.append(.nodeEvicted(nodeID, reason: .heartbeatLost))
                continue
            }

            if let lease = record.lease, !lease.isValid(at: now) {
                NodeStateMachine.apply(.leaseExpired, to: &record.state)
                storage[nodeID] = record
                events.append(.nodeEvicted(nodeID, reason: .leaseExpired))
                continue
            }
        }

        let planningSet = Set(planningMembers.map(\.nodeID))
        if planningSet != lastEpochMembers {
            let elapsed = now.timeIntervalSince(lastEpochChangedAt)
            if elapsed >= config.minimumEpochDwellTime {
                currentEpoch = currentEpoch.next()
                lastEpochChangedAt = now
                lastEpochMembers = planningSet
                reissueLeases(at: now)
                events.append(.epochChanged(currentEpoch, members: planningSet.sorted()))
            } else {
                events.append(
                    .epochChangeDeferred(untilDwellElapsed: config.minimumEpochDwellTime - elapsed)
                )
            }
        }

        return events
    }

    // MARK: - Leases

    private func issueLease(
        for profile: CapabilityProfile,
        state: NodeState,
        at now: Date,
        stabilityScore: Double
    ) -> Lease {
        // Never promise a node more time than its OS can honour. An iOS device gets a short lease
        // precisely because `willResignActive` leaves only a brief window to drain.
        let duration = min(config.leaseDuration, profile.maximumSupportableLeaseDuration)
        let stages: Set<StageType> = state.canHostRequiredStage
            ? Set(StageType.allCases)
            : Set(StageType.allCases.filter { !$0.isRequired })

        return Lease(
            nodeID: profile.nodeID,
            epoch: currentEpoch,
            issuedAt: now,
            expiresAt: now.addingTimeInterval(duration),
            allowedStageTypes: stages,
            stabilityScore: stabilityScore
        )
    }

    /// Leases are epoch-scoped, so a new generation re-issues them. A lease naming a stale epoch
    /// must never authorise work in the current one.
    private func reissueLeases(at now: Date) {
        for (nodeID, var record) in storage where record.state.participatesInPlanning {
            record.lease = issueLease(
                for: record.profile,
                state: record.state,
                at: now,
                stabilityScore: record.stabilityScore
            )
            storage[nodeID] = record
        }
    }

    private static func smoothed(_ current: Double, sample: Double) -> Double {
        current * (1 - stabilitySmoothing) + sample * stabilitySmoothing
    }
}
