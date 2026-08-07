import XCTest
@testable import ShareComputeCore

final class MembershipServiceTests: XCTestCase {

    var clock = TestClock()

    func makeService(
        config: MembershipConfig = MembershipConfig(
            leaseDuration: 60,
            heartbeatInterval: 2,
            missedHeartbeatsBeforeEviction: 3,
            minimumEpochDwellTime: 5
        )
    ) -> MembershipService {
        clock = TestClock()
        return MembershipService(localNodeID: NodeID("mac"), clock: clock, config: config)
    }

    /// Settles the initial join into a stable epoch so tests can observe the *next* change.
    func settle(_ service: MembershipService) {
        clock.advance(10)
        service.tick(at: clock.now)
    }

    // MARK: - Joining

    func testNodeJoinsAndReceivesALease() {
        let service = makeService()
        let events = service.nodeAppeared(profile: .mac("mac"))

        XCTAssertEqual(events, [.nodeJoined(NodeID("mac"))])
        let record = service.record(for: NodeID("mac"))
        XCTAssertEqual(record?.state, .activeCore)
        XCTAssertNotNil(record?.lease)
        XCTAssertTrue(record?.lease?.isValid(at: clock.now) ?? false)
    }

    /// An iOS device must never be handed a lease longer than the OS will honour, however long
    /// the configured lease duration is.
    func testIOSLeaseIsClampedToWhatTheOSCanHonour() throws {
        let service = makeService(
            config: MembershipConfig(leaseDuration: 600, minimumEpochDwellTime: 0)
        )
        service.nodeAppeared(profile: .mac("mac"))
        service.nodeAppeared(profile: .iPhone("phone"))

        let macLease = try XCTUnwrap(service.record(for: NodeID("mac"))?.lease)
        let phoneLease = try XCTUnwrap(service.record(for: NodeID("phone"))?.lease)

        XCTAssertEqual(macLease.remainingTime(at: clock.now), 300, accuracy: 0.001)
        XCTAssertEqual(phoneLease.remainingTime(at: clock.now), 30, accuracy: 0.001)
    }

    func testEpochBeginsWhenTheFirstMembersJoin() {
        let service = makeService()
        service.nodeAppeared(profile: .mac("mac"))
        XCTAssertEqual(service.epoch, .initial)

        clock.advance(10)
        let events = service.tick(at: clock.now)

        XCTAssertEqual(events, [.epochChanged(Epoch(1), members: [NodeID("mac")])])
        XCTAssertEqual(service.epoch, Epoch(1))
    }

    // MARK: - Graceful departure (the milestone's core scenario)

    /// The exact failure this milestone targets: an iPhone is backgrounded mid-generation. It
    /// must announce, drain, and be removed from the plan — never simply vanish.
    func testBackgroundedPhoneDrainsAndIsRemovedFromThePlan() {
        let service = makeService()
        service.nodeAppeared(profile: .mac("mac"))
        service.nodeAppeared(profile: .iPhone("phone"))
        settle(service)
        XCTAssertEqual(service.planningMembers.count, 2)

        let drainEvents = service.nodeAnnouncedDrain(NodeID("phone"))
        XCTAssertEqual(drainEvents, [.nodeDraining(NodeID("phone"))])
        XCTAssertEqual(service.record(for: NodeID("phone"))?.state, .draining)

        // A draining node is excluded from planning immediately, before it has finished leaving.
        XCTAssertEqual(service.planningMembers.map(\.nodeID), [NodeID("mac")])

        clock.advance(10)
        let events = service.tick(at: clock.now)

        XCTAssertTrue(events.contains(.nodeEvicted(NodeID("phone"), reason: .drained)))
        XCTAssertTrue(events.contains(.epochChanged(Epoch(2), members: [NodeID("mac")])))
        XCTAssertEqual(service.record(for: NodeID("phone"))?.state, .suspended)
    }

    // MARK: - Hard failure

    func testNodeIsEvictedOnlyAfterRepeatedHeartbeatFailures() {
        let service = makeService()
        service.nodeAppeared(profile: .mac("mac"))
        service.nodeAppeared(profile: .mac("peer"))
        settle(service)

        // One and two misses are tolerated — a single dropped packet must not re-plan the ring.
        service.heartbeatFailed(for: NodeID("peer"))
        clock.advance(1)
        XCTAssertTrue(service.tick(at: clock.now).isEmpty)

        service.heartbeatFailed(for: NodeID("peer"))
        clock.advance(1)
        XCTAssertTrue(service.tick(at: clock.now).isEmpty)
        XCTAssertEqual(service.planningMembers.count, 2)

        service.heartbeatFailed(for: NodeID("peer"))
        clock.advance(10)
        let events = service.tick(at: clock.now)

        XCTAssertTrue(events.contains(.nodeEvicted(NodeID("peer"), reason: .heartbeatLost)))
        XCTAssertEqual(service.planningMembers.map(\.nodeID), [NodeID("mac")])
    }

    func testRecoveredHeartbeatResetsTheMissCounter() {
        let service = makeService()
        service.nodeAppeared(profile: .mac("peer"))
        settle(service)

        service.heartbeatFailed(for: NodeID("peer"))
        service.heartbeatFailed(for: NodeID("peer"))
        service.heartbeatSucceeded(from: NodeID("peer"))
        service.heartbeatFailed(for: NodeID("peer"))

        clock.advance(1)
        XCTAssertTrue(service.tick(at: clock.now).isEmpty, "counter should have reset on recovery")
        XCTAssertEqual(service.record(for: NodeID("peer"))?.consecutiveMissedHeartbeats, 1)
    }

    /// Flapping should visibly degrade a node's standing even while it stays nominally alive.
    func testStabilityScoreFallsWithFailuresAndRecoversWithSuccesses() {
        let service = makeService()
        service.nodeAppeared(profile: .mac("peer"))

        let initial = service.record(for: NodeID("peer"))?.stabilityScore ?? 0
        XCTAssertEqual(initial, 1.0, accuracy: 0.001)

        service.heartbeatFailed(for: NodeID("peer"))
        service.heartbeatFailed(for: NodeID("peer"))
        let degraded = service.record(for: NodeID("peer"))?.stabilityScore ?? 1
        XCTAssertLessThan(degraded, 0.6)

        for _ in 0..<10 { service.heartbeatSucceeded(from: NodeID("peer")) }
        let recovered = service.record(for: NodeID("peer"))?.stabilityScore ?? 0
        XCTAssertGreaterThan(recovered, 0.9)
    }

    // MARK: - Lease expiry

    func testLeaseExpiryEvictsANodeThatStoppedRenewing() {
        let service = makeService()
        service.nodeAppeared(profile: .mac("mac"))
        service.nodeAppeared(profile: .iPhone("phone"))
        settle(service)

        // The phone's lease is 30s. Keep the Mac renewing; let the phone lapse without ever
        // exhausting the heartbeat budget, so this isolates lease expiry from eviction.
        clock.advance(25)
        service.heartbeatSucceeded(from: NodeID("mac"))
        XCTAssertTrue(service.tick(at: clock.now).isEmpty)

        clock.advance(10)
        service.heartbeatSucceeded(from: NodeID("mac"))
        let events = service.tick(at: clock.now)

        XCTAssertTrue(events.contains(.nodeEvicted(NodeID("phone"), reason: .leaseExpired)))
        XCTAssertEqual(service.planningMembers.map(\.nodeID), [NodeID("mac")])
    }

    func testSuccessfulHeartbeatRenewsTheLease() throws {
        let service = makeService()
        service.nodeAppeared(profile: .iPhone("phone"))
        settle(service)

        clock.advance(25)
        service.heartbeatSucceeded(from: NodeID("phone"))

        let lease = try XCTUnwrap(service.record(for: NodeID("phone"))?.lease)
        XCTAssertEqual(lease.remainingTime(at: clock.now), 30, accuracy: 0.001)

        clock.advance(10)
        XCTAssertFalse(
            service.tick(at: clock.now).contains(.nodeEvicted(NodeID("phone"), reason: .leaseExpired))
        )
    }

    // MARK: - Anti-flap

    /// Every epoch change costs a model reload, so a node flapping must not be able to drive
    /// continuous re-planning.
    func testEpochChangeIsDeferredWhileTheCurrentEpochIsTooYoung() {
        let service = makeService(
            config: MembershipConfig(missedHeartbeatsBeforeEviction: 1, minimumEpochDwellTime: 30)
        )
        service.nodeAppeared(profile: .mac("mac"))
        service.nodeAppeared(profile: .mac("peer"))

        clock.advance(40)
        XCTAssertEqual(service.tick(at: clock.now), [.epochChanged(Epoch(1), members: [NodeID("mac"), NodeID("peer")])])

        // Peer dies immediately after the epoch began.
        service.heartbeatFailed(for: NodeID("peer"))
        clock.advance(2)
        let events = service.tick(at: clock.now)

        XCTAssertTrue(events.contains(.nodeEvicted(NodeID("peer"), reason: .heartbeatLost)))
        XCTAssertEqual(service.epoch, Epoch(1), "epoch must not advance inside the dwell window")
        guard case .epochChangeDeferred = events.last else {
            return XCTFail("expected a deferral, got \(events)")
        }

        // Once the dwell elapses the change lands.
        clock.advance(30)
        let later = service.tick(at: clock.now)
        XCTAssertTrue(later.contains(.epochChanged(Epoch(2), members: [NodeID("mac")])))
    }

    func testStableMembershipProducesNoEpochChurn() {
        let service = makeService()
        service.nodeAppeared(profile: .mac("mac"))
        service.nodeAppeared(profile: .iPhone("phone"))
        settle(service)
        let epochAfterFormation = service.epoch

        for _ in 0..<50 {
            clock.advance(2)
            service.heartbeatSucceeded(from: NodeID("mac"))
            service.heartbeatSucceeded(from: NodeID("phone"))
            XCTAssertTrue(
                service.tick(at: clock.now).isEmpty,
                "a healthy ring must not emit membership events"
            )
        }
        XCTAssertEqual(service.epoch, epochAfterFormation)
    }

    // MARK: - Epoch-scoped leases

    /// A lease naming a previous epoch must not authorise work in the current one.
    func testLeasesAreReissuedAgainstTheNewEpoch() {
        let service = makeService()
        service.nodeAppeared(profile: .mac("mac"))
        service.nodeAppeared(profile: .mac("peer"))
        settle(service)
        XCTAssertEqual(service.record(for: NodeID("mac"))?.lease?.epoch, Epoch(1))

        service.nodeAnnouncedDrain(NodeID("peer"))
        clock.advance(10)
        service.tick(at: clock.now)

        XCTAssertEqual(service.epoch, Epoch(2))
        XCTAssertEqual(
            service.record(for: NodeID("mac"))?.lease?.epoch, Epoch(2),
            "surviving members must be re-leased into the new epoch"
        )
    }

    // MARK: - Profile refresh

    func testRefreshedProfileIsAdoptedWithoutRejoining() {
        let service = makeService()
        service.nodeAppeared(profile: .iPhone("phone"))
        settle(service)

        let hot = CapabilityProfile.iPhone(
            "phone",
            power: PowerProfile(isWallPowered: false, batteryFraction: 0.1, thermalState: .critical)
        )
        let events = service.nodeAppeared(profile: hot)

        XCTAssertTrue(events.isEmpty, "a refresh is not a join")
        XCTAssertTrue(service.record(for: NodeID("phone"))?.profile.power.isUnderDuress ?? false)
    }

    /// A node that is on its way out must not be revived by a stale profile broadcast.
    func testProfileRefreshDoesNotResurrectADrainingNode() {
        let service = makeService()
        service.nodeAppeared(profile: .iPhone("phone"))
        settle(service)
        service.nodeAnnouncedDrain(NodeID("phone"))

        service.nodeAppeared(profile: .iPhone("phone"))

        XCTAssertEqual(service.record(for: NodeID("phone"))?.state, .draining)
        XCTAssertTrue(service.planningMembers.isEmpty)
    }

    // MARK: - Integration with the planner

    /// End to end: a healthy two-node ring plans, the phone backgrounds, and the next plan covers
    /// every layer on the Mac alone — no gap, no stall.
    func testPlanRemainsCompleteAfterAPhoneDrains() throws {
        let service = makeService()
        let planner = StagePlanner()
        let model = ModelPlacementSpec(
            modelID: "m", layerCount: 32, totalWeightBytes: 12 * gb, perNodeOverheadBytes: 1 * gb
        )

        service.nodeAppeared(profile: .mac("mac", usableGB: 32))
        service.nodeAppeared(profile: .iPhone("phone", usableGB: 6))
        settle(service)

        let before = try planner.plan(
            model: model, members: service.planningMembers,
            epoch: service.epoch, at: clock.now, estimatedStageDuration: 5
        )
        XCTAssertEqual(before.worldSize, 2)
        XCTAssertEqual(before.assignments.map(\.layerCount).reduce(0, +), 32)

        service.nodeAnnouncedDrain(NodeID("phone"))
        clock.advance(10)
        service.tick(at: clock.now)

        let after = try planner.plan(
            model: model, members: service.planningMembers,
            epoch: service.epoch, at: clock.now, estimatedStageDuration: 5
        )
        XCTAssertEqual(after.worldSize, 1)
        XCTAssertEqual(after.assignments.map(\.layerCount).reduce(0, +), 32)
        XCTAssertEqual(after.epoch, Epoch(2))
    }
}
