import XCTest
@testable import ShareComputeCore

final class StagePlannerTests: XCTestCase {

    let clock = TestClock()
    let planner = StagePlanner()

    func validLease(
        _ node: NodeID,
        duration: TimeInterval = 300,
        stages: Set<StageType> = Set(StageType.allCases)
    ) -> Lease {
        Lease(
            nodeID: node,
            epoch: Epoch(1),
            issuedAt: clock.now,
            expiresAt: clock.now.addingTimeInterval(duration),
            allowedStageTypes: stages
        )
    }

    func member(
        _ profile: CapabilityProfile,
        state: NodeState? = nil,
        lease: Lease? = nil,
        leaseDuration: TimeInterval = 300
    ) -> PlannedMember {
        PlannedMember(
            profile: profile,
            state: state ?? NodeStateMachine.initialState(for: profile),
            lease: lease ?? validLease(profile.nodeID, duration: leaseDuration)
        )
    }

    func spec(layers: Int = 48, totalGB: Int = 20, overheadGB: Int = 0) -> ModelPlacementSpec {
        ModelPlacementSpec(
            modelID: "test-model",
            layerCount: layers,
            totalWeightBytes: totalGB * gb,
            perNodeOverheadBytes: overheadGB * gb
        )
    }

    // MARK: - Defect 1: remainder concentration on the final rank

    /// The old algorithm truncated each device's share and then gave the last rank everything
    /// left over. With a 32/6/6 GB ring and 48 layers it produced (34, 6, 8) — the final 6 GB
    /// device received 8 layers against an exact entitlement of 6.55, over-allocating the node
    /// with the least headroom by 22%.
    ///
    /// This reproduces the old arithmetic alongside the new so the regression is pinned rather
    /// than described. The guarantee that replaces it is not "equal devices get equal shards" —
    /// integer apportionment cannot promise that when the leftover is odd — but the stronger and
    /// more useful bound that *no* node deviates from its exact entitlement by a whole layer.
    func testRemainderFollowsEntitlementInsteadOfLandingOnTheLastRank() throws {
        let layers = 48
        let memories = [32, 6, 6]
        let totalMemory = memories.reduce(0, +)
        let exact = memories.map { Double(layers) * Double($0) / Double(totalMemory) }

        // Old behaviour, transcribed from ModelManager.assignShardMetadata.
        var old: [Int] = []
        var assigned = 0
        for (index, memory) in memories.enumerated() {
            let shard = layers * memory / totalMemory
            let end = index < memories.count - 1 ? assigned + shard : layers
            old.append(end - assigned)
            assigned = end
        }
        XCTAssertEqual(old, [34, 6, 8], "sanity: the old algorithm really did concentrate remainder")
        XCTAssertGreaterThan(
            Double(old[2]) - exact[2], 1.0,
            "sanity: the last rank used to overshoot its entitlement by more than a whole layer"
        )

        let new = StagePlanner.apportion(total: layers, weights: memories.map { $0 * gb })

        XCTAssertEqual(new.reduce(0, +), layers)
        XCTAssertEqual(new, [35, 7, 6])
        for (index, share) in new.enumerated() {
            XCTAssertLessThan(
                abs(Double(share) - exact[index]), 1.0,
                "node \(index) got \(share) against an entitlement of \(exact[index])"
            )
        }
    }

    /// The largest-remainder guarantee, as a property: every node receives either the floor or
    /// the ceiling of its exact proportional entitlement, so no device is ever loaded a whole
    /// layer beyond what its memory justifies.
    func testNoNodeEverDeviatesFromItsExactEntitlementByAWholeLayer() {
        var generator = SystemRandomNumberGenerator()
        for _ in 0..<500 {
            let nodeCount = Int.random(in: 2...8, using: &generator)
            let layers = Int.random(in: (nodeCount * 4)...200, using: &generator)
            let weights = (0..<nodeCount).map { _ in Int.random(in: 1...64, using: &generator) * gb }
            let totalWeight = weights.reduce(0, +)

            let result = StagePlanner.apportion(total: layers, weights: weights)

            for (index, share) in result.enumerated() {
                let exact = Double(layers) * Double(weights[index]) / Double(totalWeight)
                XCTAssertLessThanOrEqual(
                    Double(share), exact.rounded(.up),
                    "weights=\(weights) layers=\(layers) share=\(share) exact=\(exact)"
                )
            }
        }
    }

    /// Property: apportionment always sums to exactly the layer count and never starves a node.
    func testApportionmentAlwaysCoversEveryLayerAndGivesEachNodeAtLeastOne() {
        var generator = SystemRandomNumberGenerator()
        for _ in 0..<500 {
            let nodeCount = Int.random(in: 1...8, using: &generator)
            let layers = Int.random(in: nodeCount...200, using: &generator)
            let weights = (0..<nodeCount).map { _ in Int.random(in: 0...64, using: &generator) * gb }

            let result = StagePlanner.apportion(total: layers, weights: weights)

            XCTAssertEqual(result.reduce(0, +), layers, "weights=\(weights) layers=\(layers)")
            XCTAssertTrue(result.allSatisfy { $0 >= 1 }, "weights=\(weights) layers=\(layers)")
            XCTAssertEqual(result.count, nodeCount)
        }
    }

    /// Degenerate case that must not crash or silently favour rank 0.
    func testApportionmentWithZeroCapacityEverywhereSpreadsEvenly() {
        let result = StagePlanner.apportion(total: 10, weights: [0, 0, 0])
        XCTAssertEqual(result.reduce(0, +), 10)
        XCTAssertEqual(result, [4, 3, 3])
    }

    // MARK: - Defect 2: unknown memory can no longer corrupt the plan

    /// The old code substituted 1 GB for a device with no hardware profile while excluding it
    /// from the capacity total, so it contributed to the numerator but not the denominator.
    /// `PlannedMember` requires a real `MemoryProfile`, so the caller has to wait for one — the
    /// broken state is unrepresentable rather than merely avoided.
    ///
    /// This test documents the guarantee: no combination of inputs produces a short plan.
    func testEveryPlanCoversAllLayersContiguouslyWithNoGapsOrOverlaps() throws {
        let model = spec(layers: 48, totalGB: 20)
        let members = [
            member(.mac("mac", usableGB: 32)),
            member(.iPhone("phone-a", usableGB: 6)),
            member(.iPhone("phone-b", usableGB: 6)),
        ]

        let plan = try planner.plan(
            model: model,
            members: members,
            epoch: Epoch(1),
            at: clock.now,
            estimatedStageDuration: 10
        )

        XCTAssertEqual(plan.assignments.first?.startLayer, 0)
        XCTAssertEqual(plan.assignments.last?.endLayer, 48)
        XCTAssertEqual(plan.assignments.map(\.layerCount).reduce(0, +), 48)

        for (previous, next) in zip(plan.assignments, plan.assignments.dropFirst()) {
            XCTAssertEqual(previous.endLayer, next.startLayer, "shards must be contiguous")
        }
        for (index, assignment) in plan.assignments.enumerated() {
            XCTAssertEqual(assignment.rank, index)
            XCTAssertGreaterThan(assignment.layerCount, 0)
        }
    }

    // MARK: - Defect 3: per-node feasibility, not just the aggregate

    /// A ring that fits the model in total but assigns one node more than it can hold used to
    /// pass `checkIfCanLoad` and then OOM at load time.
    func testPlanFailsWhenAnIndividualNodeCannotHoldItsShard() {
        // 64 layers, 64 GB of weights, and a tiny node that will still be given its mandatory
        // layer plus a large fixed overhead it cannot absorb.
        let model = ModelPlacementSpec(
            modelID: "big",
            layerCount: 64,
            totalWeightBytes: 64 * gb,
            perNodeOverheadBytes: 3 * gb
        )
        let members = [
            member(.mac("mac", usableGB: 200)),
            member(.iPhone("tiny", usableGB: 2)),
        ]

        XCTAssertThrowsError(
            try planner.plan(
                model: model,
                members: members,
                epoch: Epoch(1),
                at: clock.now,
                estimatedStageDuration: 10
            )
        ) { error in
            guard case let PlanningError.nodeCannotFitShard(node, _, usable) = error else {
                return XCTFail("expected nodeCannotFitShard, got \(error)")
            }
            XCTAssertEqual(node, NodeID("tiny"))
            XCTAssertEqual(usable, 2 * gb)
        }
    }

    func testPlanFailsWhenAggregateMemoryIsInsufficient() {
        let model = spec(layers: 48, totalGB: 500)
        let members = [member(.mac("mac", usableGB: 32))]

        XCTAssertThrowsError(
            try planner.plan(
                model: model,
                members: members,
                epoch: Epoch(1),
                at: clock.now,
                estimatedStageDuration: 10
            )
        ) { error in
            guard case PlanningError.insufficientAggregateMemory = error else {
                return XCTFail("expected insufficientAggregateMemory, got \(error)")
            }
        }
    }

    func testPlanFailsWhenThereAreMoreNodesThanLayers() {
        let model = spec(layers: 2, totalGB: 1)
        let members = [
            member(.mac("a")), member(.mac("b")), member(.mac("c")),
        ]

        XCTAssertThrowsError(
            try planner.plan(
                model: model,
                members: members,
                epoch: Epoch(1),
                at: clock.now,
                estimatedStageDuration: 10
            )
        ) { error in
            guard case PlanningError.moreNodesThanLayers = error else {
                return XCTFail("expected moreNodesThanLayers, got \(error)")
            }
        }
    }

    // MARK: - Eligibility (spec §9.1)

    func testDrainingAndSuspendedNodesAreNeverPlanned() {
        let members = [
            member(.mac("healthy")),
            member(.mac("leaving"), state: .draining),
            member(.mac("gone"), state: .suspended),
        ]
        let eligible = planner.eligibleMembers(
            from: members, at: clock.now, estimatedStageDuration: 10
        )
        XCTAssertEqual(eligible.map(\.nodeID), [NodeID("healthy")])
    }

    func testNodeWithoutALeaseIsNotPlanned() {
        let member = PlannedMember(profile: .mac("no-lease"), state: .activeCore, lease: nil)
        XCTAssertTrue(
            planner.eligibleMembers(from: [member], at: clock.now, estimatedStageDuration: 10).isEmpty
        )
    }

    /// Spec §9.1: never place a stage that is expected to outlive the lease.
    func testNodeIsNotPlannedWhenTheStageWouldOutliveItsLease() {
        let members = [member(.iPhone("phone"), leaseDuration: 30)]

        XCTAssertEqual(
            planner.eligibleMembers(from: members, at: clock.now, estimatedStageDuration: 20).count,
            1
        )
        XCTAssertTrue(
            planner.eligibleMembers(from: members, at: clock.now, estimatedStageDuration: 45).isEmpty,
            "a stage longer than the remaining lease must not be placed"
        )
    }

    func testExpiredLeaseMakesNodeIneligible() {
        let members = [member(.mac("mac"), leaseDuration: 60)]
        XCTAssertEqual(
            planner.eligibleMembers(from: members, at: clock.now, estimatedStageDuration: 10).count, 1
        )

        clock.advance(61)
        XCTAssertTrue(
            planner.eligibleMembers(from: members, at: clock.now, estimatedStageDuration: 10).isEmpty
        )
    }

    func testNodesUnderPowerDuressAreExcluded() {
        let hot = CapabilityProfile.mac(
            "hot",
            power: PowerProfile(isWallPowered: true, thermalState: .serious)
        )
        let drained = CapabilityProfile.iPhone(
            "drained",
            power: PowerProfile(isWallPowered: false, batteryFraction: 0.05)
        )
        let members = [member(.mac("fine")), member(hot), member(drained)]

        let eligible = planner.eligibleMembers(
            from: members, at: clock.now, estimatedStageDuration: 10
        )
        XCTAssertEqual(eligible.map(\.nodeID), [NodeID("fine")])
    }

    func testUntrustedAndNonConsentingNodesAreExcluded() {
        let members = [
            member(.mac("trusted")),
            member(.mac("untrusted", trust: .semiTrustedEdge)),
            member(.mac("no-consent", declaresCanHostRequiredStage: false)),
            member(.opportunistic("tablet")),
        ]
        let eligible = planner.eligibleMembers(
            from: members, at: clock.now, estimatedStageDuration: 10
        )
        XCTAssertEqual(eligible.map(\.nodeID), [NodeID("trusted")])
    }

    func testPlanThrowsWhenNothingIsEligible() {
        let members = [member(.mac("leaving"), state: .draining)]
        XCTAssertThrowsError(
            try planner.plan(
                model: spec(),
                members: members,
                epoch: Epoch(1),
                at: clock.now,
                estimatedStageDuration: 10
            )
        ) { error in
            guard case PlanningError.noEligibleNodes = error else {
                return XCTFail("expected noEligibleNodes, got \(error)")
            }
        }
    }

    // MARK: - Determinism

    /// Every node must derive the same ranks from the same facts, whatever order it learned them.
    func testPlanIsIndependentOfMemberOrdering() throws {
        let model = spec(layers: 40, totalGB: 16)
        let profiles: [CapabilityProfile] = [
            .mac("zulu", usableGB: 16),
            .iPhone("alpha", usableGB: 8),
            .mac("mike", usableGB: 24),
        ]
        let forward = try planner.plan(
            model: model,
            members: profiles.map { member($0) },
            epoch: Epoch(3),
            at: clock.now,
            estimatedStageDuration: 5
        )
        let reversed = try planner.plan(
            model: model,
            members: profiles.reversed().map { member($0) },
            epoch: Epoch(3),
            at: clock.now,
            estimatedStageDuration: 5
        )
        XCTAssertEqual(forward, reversed)
        XCTAssertEqual(
            forward.assignments.map(\.nodeID),
            [NodeID("alpha"), NodeID("mike"), NodeID("zulu")]
        )
    }

    /// The heterogeneous ring the README actually advertises: a Mac plus an iPhone.
    func testMacPlusIPhoneSplitsProportionallyToUsableMemory() throws {
        let model = spec(layers: 36, totalGB: 18)
        let plan = try planner.plan(
            model: model,
            members: [member(.mac("mac", usableGB: 30)), member(.iPhone("phone", usableGB: 6))],
            epoch: Epoch(1),
            at: clock.now,
            estimatedStageDuration: 5
        )

        let mac = try XCTUnwrap(plan.assignment(for: NodeID("mac")))
        let phone = try XCTUnwrap(plan.assignment(for: NodeID("phone")))

        XCTAssertEqual(mac.layerCount + phone.layerCount, 36)
        XCTAssertGreaterThan(mac.layerCount, phone.layerCount)
        XCTAssertEqual(mac.layerCount, 30)
        XCTAssertEqual(phone.layerCount, 6)
    }
}
