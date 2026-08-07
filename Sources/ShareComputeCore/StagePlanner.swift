import Foundation

/// What a model costs to host, independent of how it is split.
public struct ModelPlacementSpec: Sendable, Equatable {
    public let modelID: String
    public let layerCount: Int
    public let totalWeightBytes: Int
    /// Memory each participating node needs regardless of how many layers it holds — runtime,
    /// activations, KV cache, embeddings. Ignoring this is how a plan passes an aggregate check
    /// and then OOMs at load.
    public let perNodeOverheadBytes: Int

    public init(
        modelID: String,
        layerCount: Int,
        totalWeightBytes: Int,
        perNodeOverheadBytes: Int
    ) {
        self.modelID = modelID
        self.layerCount = layerCount
        self.totalWeightBytes = totalWeightBytes
        self.perNodeOverheadBytes = perNodeOverheadBytes
    }

    /// Rounded up: under-estimating per-layer cost is the dangerous direction.
    public var bytesPerLayer: Int {
        guard layerCount > 0 else { return 0 }
        return (totalWeightBytes + layerCount - 1) / layerCount
    }

    public func bytesFor(layers: Int) -> Int {
        layers * bytesPerLayer + perNodeOverheadBytes
    }
}

/// A node offered to the planner.
///
/// Memory is non-optional by construction. The current implementation substitutes a 1 GB guess
/// for a device whose hardware profile has not arrived yet, while excluding that same device from
/// the capacity total — so it contributes to the numerator but not the denominator and the plan
/// silently goes wrong (`findings.md` F5.2). Making the profile mandatory here removes that state
/// from the type system: a caller with no profile must wait for one, not invent it.
public struct PlannedMember: Sendable, Equatable {
    public let profile: CapabilityProfile
    public let state: NodeState
    public let lease: Lease?

    public init(profile: CapabilityProfile, state: NodeState, lease: Lease? = nil) {
        self.profile = profile
        self.state = state
        self.lease = lease
    }

    public var nodeID: NodeID { profile.nodeID }

    /// Bytes available for model layers after fixed overhead.
    func capacityForLayers(overhead: Int) -> Int {
        max(0, profile.memory.usableBytes - overhead)
    }
}

public struct EligibilityPolicy: Sendable, Equatable {
    /// Minimum trust to hold authoritative state. Spec §9.1.
    public var minimumTrustForRequiredStage: TrustLevel
    /// Require a valid lease with room for the whole stage before placing work. Spec §9.1.
    public var requireLeaseHeadroom: Bool
    /// Exclude nodes that are thermally or battery constrained. Spec §9.3.
    public var excludeNodesUnderPowerDuress: Bool

    public init(
        minimumTrustForRequiredStage: TrustLevel = .trustedCore,
        requireLeaseHeadroom: Bool = true,
        excludeNodesUnderPowerDuress: Bool = true
    ) {
        self.minimumTrustForRequiredStage = minimumTrustForRequiredStage
        self.requireLeaseHeadroom = requireLeaseHeadroom
        self.excludeNodesUnderPowerDuress = excludeNodesUnderPowerDuress
    }

    public static let `default` = EligibilityPolicy()
}

public struct ShardAssignment: Sendable, Equatable {
    public let nodeID: NodeID
    public let rank: Int
    public let startLayer: Int
    /// Exclusive, matching `ShardMetadata.endLayer` in the existing app.
    public let endLayer: Int
    public let estimatedBytes: Int

    public var layerCount: Int { endLayer - startLayer }
    public var isFirstShard: Bool { startLayer == 0 }
}

public struct ShardPlan: Sendable, Equatable {
    public let epoch: Epoch
    public let modelID: String
    public let layerCount: Int
    /// Sorted by rank, contiguous, covering exactly `0..<layerCount`.
    public let assignments: [ShardAssignment]

    public var worldSize: Int { assignments.count }

    public func assignment(for nodeID: NodeID) -> ShardAssignment? {
        assignments.first { $0.nodeID == nodeID }
    }
}

public enum PlanningError: Error, Equatable, CustomStringConvertible {
    case noEligibleNodes
    case moreNodesThanLayers(layers: Int, nodes: Int)
    case insufficientAggregateMemory(requiredBytes: Int, availableBytes: Int)
    case nodeCannotFitShard(NodeID, requiredBytes: Int, usableBytes: Int)

    public var description: String {
        switch self {
        case .noEligibleNodes:
            return "No node satisfies the placement rules for a required stage"
        case let .moreNodesThanLayers(layers, nodes):
            return "Cannot split \(layers) layers across \(nodes) nodes — every node must hold at least one layer"
        case let .insufficientAggregateMemory(required, available):
            return "Ring has \(available) usable bytes, model needs \(required)"
        case let .nodeCannotFitShard(node, required, usable):
            return "Node \(node) was assigned \(required) bytes but can only hold \(usable)"
        }
    }
}

/// Assigns contiguous layer ranges to nodes, proportional to usable memory.
///
/// Replaces `ModelManager.assignShardMetadata`, which had three defects this type is built to
/// avoid (see `findings.md` F5):
///
/// 1. It used truncating integer division per device and then gave the final rank
///    `endLayer = nLayers`, concentrating every device's accumulated rounding error on one node.
///    Here apportionment uses the largest-remainder method, so the shards sum to exactly
///    `layerCount` and leftover layers go to the nodes with the largest fractional entitlement.
/// 2. It invented a 1 GB memory figure for nodes whose profile had not arrived, while omitting
///    them from the capacity total. `PlannedMember` requires a real profile, so that state cannot
///    be constructed.
/// 3. It checked only that the model fitted the *sum* of ring memory. Here every node is checked
///    against its own assignment.
public struct StagePlanner: Sendable {
    public let policy: EligibilityPolicy

    public init(policy: EligibilityPolicy = .default) {
        self.policy = policy
    }

    /// Nodes that may hold a required stage right now.
    ///
    /// In this architecture pipeline parallelism means every rank in the group participates in
    /// the authoritative forward pass, so a node that is not fit to hold a required stage is not
    /// fit to be in the group at all.
    public func eligibleMembers(
        from members: [PlannedMember],
        at now: Date,
        estimatedStageDuration: TimeInterval
    ) -> [PlannedMember] {
        members.filter { member in
            guard member.state.canHostRequiredStage else { return false }
            guard member.profile.declaresCanHostRequiredStage else { return false }
            guard member.profile.trust >= policy.minimumTrustForRequiredStage else { return false }

            if policy.excludeNodesUnderPowerDuress, member.profile.power.isUnderDuress {
                return false
            }

            if policy.requireLeaseHeadroom {
                guard let lease = member.lease else { return false }
                guard lease.canHost(
                    .decodeAuthoritative,
                    estimatedDuration: estimatedStageDuration,
                    at: now
                ) else { return false }
            }

            return true
        }
        // Deterministic order so every node derives the same ranks from the same inputs.
        .sorted { $0.nodeID < $1.nodeID }
    }

    public func plan(
        model: ModelPlacementSpec,
        members: [PlannedMember],
        epoch: Epoch,
        at now: Date,
        estimatedStageDuration: TimeInterval
    ) throws -> ShardPlan {
        let eligible = eligibleMembers(
            from: members,
            at: now,
            estimatedStageDuration: estimatedStageDuration
        )
        guard !eligible.isEmpty else { throw PlanningError.noEligibleNodes }
        guard eligible.count <= model.layerCount else {
            throw PlanningError.moreNodesThanLayers(
                layers: model.layerCount,
                nodes: eligible.count
            )
        }

        let capacities = eligible.map { $0.capacityForLayers(overhead: model.perNodeOverheadBytes) }
        let available = capacities.reduce(0, +)
        guard available >= model.totalWeightBytes else {
            throw PlanningError.insufficientAggregateMemory(
                requiredBytes: model.totalWeightBytes,
                availableBytes: available
            )
        }

        let layerCounts = Self.apportion(
            total: model.layerCount,
            weights: capacities
        )

        var assignments: [ShardAssignment] = []
        var cursor = 0
        for (rank, member) in eligible.enumerated() {
            let layers = layerCounts[rank]
            let bytes = model.bytesFor(layers: layers)

            // Defect 3: per-node feasibility, not just the aggregate.
            guard bytes <= member.profile.memory.usableBytes else {
                throw PlanningError.nodeCannotFitShard(
                    member.nodeID,
                    requiredBytes: bytes,
                    usableBytes: member.profile.memory.usableBytes
                )
            }

            assignments.append(
                ShardAssignment(
                    nodeID: member.nodeID,
                    rank: rank,
                    startLayer: cursor,
                    endLayer: cursor + layers,
                    estimatedBytes: bytes
                )
            )
            cursor += layers
        }

        // Structural guarantee the old code could not make: full, contiguous coverage.
        precondition(cursor == model.layerCount, "shard plan must cover every layer exactly once")

        return ShardPlan(
            epoch: epoch,
            modelID: model.modelID,
            layerCount: model.layerCount,
            assignments: assignments
        )
    }

    /// Largest-remainder (Hamilton) apportionment.
    ///
    /// Guarantees `result.reduce(0,+) == total`, `result.allSatisfy { $0 >= 1 }`, and — the
    /// property that actually protects memory-constrained devices — that every node receives
    /// either the floor or the ceiling of its exact proportional entitlement. The old algorithm
    /// offered no such bound: it truncated every share and gave the last rank the accumulated
    /// remainder, which on a 32/6/6 GB ring over-allocated a 6 GB device by 22%.
    ///
    /// The minimum-one-layer rule is applied *after* apportionment, by moving a layer from the
    /// largest holder. Granting the floor of one layer up front and dividing the rest would bias
    /// every share toward the smallest node — the same failure mode in a milder form.
    static func apportion(total: Int, weights: [Int]) -> [Int] {
        let count = weights.count
        precondition(count > 0)
        precondition(total >= count, "caller must reject rings with more nodes than layers")

        let totalWeight = weights.reduce(0, +)
        var result: [Int]

        if totalWeight <= 0 {
            // Degenerate but representable: nobody has spare capacity. Spread evenly rather than
            // silently favouring the first ranks.
            result = Array(repeating: total / count, count: count)
            for index in 0..<(total % count) {
                result[index] += 1
            }
        } else {
            result = Array(repeating: 0, count: count)
            var fractions: [(index: Int, fraction: Double)] = []
            var distributed = 0

            for (index, weight) in weights.enumerated() {
                let exact = Double(total) * Double(weight) / Double(totalWeight)
                let whole = Int(exact.rounded(.down))
                result[index] = whole
                distributed += whole
                fractions.append((index, exact - Double(whole)))
            }

            // Hand out what rounding left over, largest fractional entitlement first. Ties break
            // on index so every node derives the same answer from the same inputs.
            let leftover = total - distributed
            if leftover > 0 {
                let ordered = fractions.sorted {
                    $0.fraction == $1.fraction ? $0.index < $1.index : $0.fraction > $1.fraction
                }
                for offset in 0..<leftover {
                    result[ordered[offset].index] += 1
                }
            }
        }

        // A node holding zero layers would still take part in every per-token collective while
        // contributing nothing. `total >= count` guarantees this can always be satisfied.
        while let starved = result.indices.first(where: { result[$0] == 0 }) {
            guard let largest = result.indices.max(by: { result[$0] < result[$1] }),
                  result[largest] > 1 else { break }
            result[largest] -= 1
            result[starved] += 1
        }

        return result
    }
}
