import Foundation

/// Outcome of a pool membership change that adapters (and the demo) can observe.
public enum PoolConnectionEvent: Sendable, Equatable {
    case peerConnected(platform: PlatformKind, nodeID: NodeID)
    case peerAlreadyPresent(platform: PlatformKind, nodeID: NodeID)
    case peerDraining(platform: PlatformKind, nodeID: NodeID)
    case peerEvicted(platform: PlatformKind, nodeID: NodeID, reason: EvictionReason)
    case allRequiredPlatformsConnected(Set<PlatformKind>)
    case epochChanged(Epoch, members: [NodeID])
    case epochChangeDeferred(untilDwellElapsed: TimeInterval)
}

/// Result of attempting a RAM-pooling plan once platforms are present.
public struct PoolPlanResult: Sendable, Equatable {
    public let platforms: [PlatformKind]
    public let plan: ShardPlan
    public let totalUsableBytes: Int

    public init(platforms: [PlatformKind], plan: ShardPlan, totalUsableBytes: Int) {
        self.platforms = platforms
        self.plan = plan
        self.totalUsableBytes = totalUsableBytes
    }
}

/// Coordinates membership across Windows / macOS / iOS / Android peers.
///
/// Wraps `MembershipService` + `StagePlanner` with an explicit platform roster so the success
/// criterion "all four platforms connected" is a first-class check rather than an accident of
/// node IDs. Peers may be real adapters or `SimulatedPlatformPeer` stand-ins.
///
/// Still performs no I/O: the host calls `connect` / `heartbeatSucceeded` / `tick`, and optionally
/// mirrors events onto a `RingTransport`. When a transport is injected, the pool **owns** it for
/// the pool lifetime (strong reference) so join/heartbeat/drain/epoch notifications keep working
/// after `init` returns.
public final class CrossPlatformPool {
    public let localNodeID: NodeID
    public let membership: MembershipService
    public let planner: StagePlanner

    private let clock: RingClock
    private var platformByNode: [NodeID: PlatformKind] = [:]
    private var nodeByPlatform: [PlatformKind: NodeID] = [:]
    /// Strong ownership: callers may pass `CrossPlatformPool(…, transport: SocketTransport())`
    /// without separately retaining the transport.
    private let transport: RingTransport?

    public init(
        localNodeID: NodeID = NodeID("pool-hub"),
        clock: RingClock,
        config: MembershipConfig = .default,
        planner: StagePlanner = StagePlanner(),
        transport: RingTransport? = nil
    ) {
        self.localNodeID = localNodeID
        self.clock = clock
        self.membership = MembershipService(
            localNodeID: localNodeID,
            clock: clock,
            config: config
        )
        self.planner = planner
        self.transport = transport
    }

    // MARK: - Observation

    public var connectedPlatforms: Set<PlatformKind> {
        Set(nodeByPlatform.keys)
    }

    public var missingPlatforms: Set<PlatformKind> {
        PlatformKind.allRequired.subtracting(connectedPlatforms)
    }

    public var hasAllRequiredPlatforms: Bool {
        missingPlatforms.isEmpty
    }

    public func platform(for nodeID: NodeID) -> PlatformKind? {
        platformByNode[nodeID]
    }

    public func nodeID(for platform: PlatformKind) -> NodeID? {
        nodeByPlatform[platform]
    }

    // MARK: - Connect

    /// Register a peer for `platform`. If that platform already has a live planning member, the
    /// existing node is refreshed rather than duplicated — one seat per platform in this milestone.
    @discardableResult
    public func connect(
        platform: PlatformKind,
        profile: CapabilityProfile
    ) -> [PoolConnectionEvent] {
        var events: [PoolConnectionEvent] = []

        if let existing = nodeByPlatform[platform], existing != profile.nodeID {
            // Replace the previous occupant of this platform seat.
            _ = membership.nodeAnnouncedDrain(existing)
            platformByNode.removeValue(forKey: existing)
        }

        if nodeByPlatform[platform] == profile.nodeID {
            _ = membership.nodeAppeared(profile: profile)
            events.append(.peerAlreadyPresent(platform: platform, nodeID: profile.nodeID))
        } else {
            // MembershipService reactivates a previously suspended NodeID on appear; do that
            // before restoring the platform seat so peerConnected implies planning membership.
            let joinEvents = membership.nodeAppeared(profile: profile)
            platformByNode[profile.nodeID] = platform
            nodeByPlatform[platform] = profile.nodeID
            if joinEvents.contains(where: {
                if case .nodeJoined = $0 { return true }
                return false
            }) || membership.record(for: profile.nodeID) != nil {
                events.append(.peerConnected(platform: platform, nodeID: profile.nodeID))
            }
            try? transport?.broadcast(.join(platform: platform, profile: profile))
            try? transport?.send(to: profile.nodeID, message: .acknowledged(profile.nodeID))
        }

        if hasAllRequiredPlatforms {
            events.append(.allRequiredPlatformsConnected(connectedPlatforms))
        }

        return events
    }

    /// Connect a stock simulated peer for `platform` (usable for demos and tests).
    @discardableResult
    public func connectSimulated(
        _ platform: PlatformKind,
        usableGB: Int? = nil
    ) -> [PoolConnectionEvent] {
        let profile = SimulatedPlatformPeer.profile(for: platform, usableGB: usableGB)
        return connect(platform: platform, profile: profile)
    }

    /// Connect the full Windows / macOS / iOS / Android set with default simulated profiles.
    @discardableResult
    public func connectAllSimulatedPlatforms() -> [PoolConnectionEvent] {
        var events: [PoolConnectionEvent] = []
        for platform in PlatformKind.allCases.sorted() {
            events.append(contentsOf: connectSimulated(platform))
        }
        return events
    }

    public func heartbeatSucceeded(from nodeID: NodeID) {
        membership.heartbeatSucceeded(from: nodeID)
        try? transport?.send(to: nodeID, message: .heartbeat(nodeID))
    }

    public func heartbeatFailed(for nodeID: NodeID) {
        membership.heartbeatFailed(for: nodeID)
    }

    @discardableResult
    public func announceDrain(_ nodeID: NodeID) -> [PoolConnectionEvent] {
        let membershipEvents = membership.nodeAnnouncedDrain(nodeID)
        var events: [PoolConnectionEvent] = []
        if let platform = platformByNode[nodeID],
           membershipEvents.contains(where: {
               if case .nodeDraining = $0 { return true }
               return false
           }) {
            events.append(.peerDraining(platform: platform, nodeID: nodeID))
            try? transport?.broadcast(.drain(nodeID))
        }
        return events
    }

    @discardableResult
    public func tick(at now: Date? = nil) -> [PoolConnectionEvent] {
        let instant = now ?? clock.now
        let membershipEvents = membership.tick(at: instant)
        var events: [PoolConnectionEvent] = []

        for event in membershipEvents {
            switch event {
            case let .nodeEvicted(nodeID, reason):
                if let platform = platformByNode[nodeID] {
                    events.append(.peerEvicted(platform: platform, nodeID: nodeID, reason: reason))
                    platformByNode.removeValue(forKey: nodeID)
                    if nodeByPlatform[platform] == nodeID {
                        nodeByPlatform.removeValue(forKey: platform)
                    }
                }
            case let .epochChanged(epoch, members):
                events.append(.epochChanged(epoch, members: members))
                try? transport?.broadcast(.epochChanged(epoch, members: members))
            case let .epochChangeDeferred(until):
                events.append(.epochChangeDeferred(untilDwellElapsed: until))
            case .nodeJoined, .nodeDraining:
                break
            }
        }

        return events
    }

    // MARK: - RAM pooling

    /// When all four platforms are connected and the membership epoch is ready, produce a shard
    /// plan that pools their usable RAM. Returns `nil` if platforms are still missing, if the
    /// pending membership change is still deferred by anti-flap dwell, or if any required
    /// platform is ineligible for assignment.
    public func planRAMPool(
        model: ModelPlacementSpec,
        estimatedStageDuration: TimeInterval = 10,
        at now: Date? = nil
    ) throws -> PoolPlanResult? {
        guard hasAllRequiredPlatforms else { return nil }

        let instant = now ?? clock.now
        // Advance membership so the first epoch after the four joins can fire past anti-flap dwell.
        let tickEvents = tick(at: instant)

        // Anti-flap: never return a plan for new membership under the old epoch.
        if tickEvents.contains(where: {
            if case .epochChangeDeferred = $0 { return true }
            return false
        }) {
            return nil
        }

        // Tick may have evicted a required seat (lease expiry / heartbeat loss).
        guard hasAllRequiredPlatforms else { return nil }

        let members = membership.planningMembers
        guard !members.isEmpty else { return nil }

        let plan = try planner.plan(
            model: model,
            members: members,
            epoch: membership.epoch,
            at: instant,
            estimatedStageDuration: estimatedStageDuration
        )

        // Report only members that actually received shards; reject incomplete four-platform sets.
        let assignedNodeIDs = Set(plan.assignments.map(\.nodeID))
        let assignedMembers = members.filter { assignedNodeIDs.contains($0.nodeID) }
        let platforms = assignedMembers.compactMap { platformByNode[$0.nodeID] }.sorted()
        guard Set(platforms) == PlatformKind.allRequired else { return nil }

        let totalUsable = assignedMembers.reduce(0) { $0 + $1.profile.memory.usableBytes }

        return PoolPlanResult(platforms: platforms, plan: plan, totalUsableBytes: totalUsable)
    }
}

/// Stock capability profiles for simulated Windows / macOS / iOS / Android peers.
public enum SimulatedPlatformPeer {
    public static func profile(
        for platform: PlatformKind,
        name: String? = nil,
        usableGB: Int? = nil
    ) -> CapabilityProfile {
        let gb = 1024 * 1024 * 1024
        let nodeName = name ?? "sim-\(platform.rawValue)"

        switch platform {
        case .windows:
            let usable = usableGB ?? 16
            return CapabilityProfile(
                nodeID: NodeID(nodeName),
                connectivity: .coreDesktop,
                backgroundLink: .desktopUnrestricted,
                memory: MemoryProfile(
                    totalBytes: usable * gb,
                    usableBytes: usable * gb,
                    reclaimModel: .windowsWorkingSetTrim
                ),
                runtimeBackends: [.winmlDirectML, .onnxRuntime, .llamaCppCPU],
                power: .wallPowered,
                trust: .trustedCore,
                declaresCanHostRequiredStage: true
            )
        case .macos:
            let usable = usableGB ?? 32
            return CapabilityProfile(
                nodeID: NodeID(nodeName),
                connectivity: .coreDesktop,
                backgroundLink: .desktopUnrestricted,
                memory: MemoryProfile(
                    totalBytes: usable * gb,
                    usableBytes: usable * gb,
                    reclaimModel: .appleUnifiedMemoryPressure
                ),
                runtimeBackends: [.mlxMetal, .llamaCppGPU],
                power: .wallPowered,
                trust: .trustedCore,
                declaresCanHostRequiredStage: true
            )
        case .ios:
            let usable = usableGB ?? 6
            return CapabilityProfile(
                nodeID: NodeID(nodeName),
                connectivity: .elasticMobile,
                backgroundLink: .iosSuspended,
                memory: MemoryProfile(
                    totalBytes: usable * gb,
                    usableBytes: usable * gb,
                    reclaimModel: .iosJetsam
                ),
                runtimeBackends: [.mlxMetal, .coreMLANE],
                power: PowerProfile(isWallPowered: false, batteryFraction: 0.8),
                trust: .trustedCore,
                declaresCanHostRequiredStage: true
            )
        case .android:
            let usable = usableGB ?? 8
            return CapabilityProfile(
                nodeID: NodeID(nodeName),
                connectivity: .elasticMobile,
                backgroundLink: .androidForegroundService,
                memory: MemoryProfile(
                    totalBytes: usable * gb,
                    usableBytes: usable * gb,
                    reclaimModel: .linuxCgroupOOM
                ),
                runtimeBackends: [.liteRT, .llamaCppCPU, .onnxRuntime],
                power: PowerProfile(isWallPowered: false, batteryFraction: 0.7),
                trust: .trustedCore,
                declaresCanHostRequiredStage: true
            )
        }
    }
}
