import XCTest
@testable import ShareComputeCore

final class FourPlatformPoolTests: XCTestCase {

    func makePool(
        dwell: TimeInterval = 0,
        transport: RingTransport? = nil
    ) -> (CrossPlatformPool, TestClock) {
        let clock = TestClock()
        let pool = CrossPlatformPool(
            clock: clock,
            config: MembershipConfig(
                leaseDuration: 60,
                heartbeatInterval: 2,
                missedHeartbeatsBeforeEviction: 3,
                minimumEpochDwellTime: dwell
            ),
            transport: transport
        )
        return (pool, clock)
    }

    func testSimulatedWindowsMacOSIOSAndroidAllConnect() {
        let (pool, _) = makePool()
        let events = pool.connectAllSimulatedPlatforms()

        XCTAssertEqual(pool.connectedPlatforms, PlatformKind.allRequired)
        XCTAssertTrue(pool.missingPlatforms.isEmpty)
        XCTAssertTrue(pool.hasAllRequiredPlatforms)
        XCTAssertTrue(events.contains(.allRequiredPlatformsConnected(PlatformKind.allRequired)))

        for platform in PlatformKind.allCases {
            XCTAssertNotNil(pool.nodeID(for: platform), "missing seat for \(platform)")
        }
    }

    func testRAMPoolPlanCoversEveryPlatformSeat() throws {
        let (pool, clock) = makePool()
        _ = pool.connectAllSimulatedPlatforms()
        for nodeID in pool.membership.members.keys {
            pool.heartbeatSucceeded(from: nodeID)
        }
        clock.advance(1)

        let model = ModelPlacementSpec(
            modelID: "test-32L",
            layerCount: 32,
            totalWeightBytes: 16 * gb,
            perNodeOverheadBytes: 256 * 1024 * 1024
        )

        let result = try XCTUnwrap(
            try pool.planRAMPool(model: model, estimatedStageDuration: 5, at: clock.now)
        )

        XCTAssertEqual(Set(result.platforms), PlatformKind.allRequired)
        XCTAssertEqual(result.plan.worldSize, 4)
        XCTAssertEqual(result.plan.assignments.map(\.layerCount).reduce(0, +), 32)
        XCTAssertEqual(result.plan.assignments.first?.startLayer, 0)
        XCTAssertEqual(result.plan.assignments.last?.endLayer, 32)
    }

    func testMissingPlatformBlocksThePoolPlan() throws {
        let (pool, clock) = makePool()

        _ = pool.connectSimulated(.windows)
        _ = pool.connectSimulated(.macos)
        _ = pool.connectSimulated(.ios)
        // Android deliberately absent

        XCTAssertEqual(pool.missingPlatforms, [.android])
        XCTAssertFalse(pool.hasAllRequiredPlatforms)

        let model = ModelPlacementSpec(
            modelID: "test",
            layerCount: 16,
            totalWeightBytes: 4 * gb,
            perNodeOverheadBytes: 64 * 1024 * 1024
        )
        let result = try pool.planRAMPool(model: model, at: clock.now)
        XCTAssertNil(result)
    }

    func testInProcessTransportRecordsJoins() {
        let transport = InProcessRingTransport()
        let (pool, _) = makePool(transport: transport)

        _ = pool.connectSimulated(.android)
        XCTAssertFalse(transport.broadcastLog.isEmpty)

        guard case let .join(platform, profile) = transport.broadcastLog.first else {
            return XCTFail("expected join broadcast, got \(transport.broadcastLog)")
        }
        XCTAssertEqual(platform, .android)
        XCTAssertEqual(profile.nodeID, NodeID("sim-android"))
    }

    func testSimulatedPlatformPeerProfilesMatchCapabilities() {
        let windows = SimulatedPlatformPeer.profile(for: .windows)
        XCTAssertEqual(windows.connectivity, .coreDesktop)
        XCTAssertEqual(windows.memory.reclaimModel, .windowsWorkingSetTrim)
        XCTAssertTrue(windows.runtimeBackends.contains(.winmlDirectML))

        let android = SimulatedPlatformPeer.profile(for: .android)
        XCTAssertEqual(android.connectivity, .elasticMobile)
        XCTAssertEqual(android.backgroundLink, .androidForegroundService)
        XCTAssertTrue(android.runtimeBackends.contains(.liteRT))

        let ios = SimulatedPlatformPeer.profile(for: .ios)
        XCTAssertEqual(ios.backgroundLink, .iosSuspended)
        XCTAssertEqual(ios.maximumSupportableLeaseDuration, 30)

        let macos = SimulatedPlatformPeer.profile(for: .macos)
        XCTAssertEqual(macos.connectivity, .coreDesktop)
        XCTAssertTrue(macos.runtimeBackends.contains(.mlxMetal))
    }
}
