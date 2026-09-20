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

    func testModel() -> ModelPlacementSpec {
        ModelPlacementSpec(
            modelID: "test-32L",
            layerCount: 32,
            totalWeightBytes: 16 * gb,
            perNodeOverheadBytes: 256 * 1024 * 1024
        )
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

        let result = try XCTUnwrap(
            try pool.planRAMPool(model: testModel(), estimatedStageDuration: 5, at: clock.now)
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

    func testTcpRingTransportFailsWhenNotConnected() {
        let transport = TcpRingTransport(isConnected: false)
        XCTAssertThrowsError(try transport.broadcast(.heartbeat(NodeID("x")))) { error in
            XCTAssertTrue(error is TcpRingTransport.TransportError)
        }
        transport.isConnected = true
        XCTAssertNoThrow(try transport.broadcast(.heartbeat(NodeID("x"))))
        XCTAssertEqual(transport.broadcasts.count, 1)
    }

    func testTcpRingProtocolJoinLineRoundTrip() throws {
        let line = try TcpRingProtocol.encodeJoinLine(
            platform: .android,
            nodeID: "sim-android",
            usableGB: 8
        )
        // Strip trailing newline for decode helper.
        let stripped = line.dropLast()
        let decoded = try TcpRingProtocol.decodeJoinLine(Data(stripped))
        XCTAssertEqual(decoded.type, "join")
        XCTAssertEqual(decoded.platform, .android)
        XCTAssertEqual(decoded.nodeID, "sim-android")
        XCTAssertEqual(decoded.usableGB, 8)
        XCTAssertEqual(decoded.v, TcpRingProtocol.version)

        // Wire keys must match the Python demo (`node_id`, `usable_gb`).
        let json = try XCTUnwrap(String(data: Data(stripped), encoding: .utf8))
        XCTAssertTrue(json.contains("\"node_id\""))
        XCTAssertTrue(json.contains("\"usable_gb\""))
        XCTAssertTrue(json.contains("\"platform\":\"android\""))
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

    // MARK: - ReviewAgent finding coverage

    /// Finding 1: deferred epoch → nil (do not plan new membership under old epoch).
    func testPlanRAMPoolReturnsNilWhileEpochChangeDeferred() throws {
        let (pool, clock) = makePool(dwell: 5)
        _ = pool.connectAllSimulatedPlatforms()
        for nodeID in pool.membership.members.keys {
            pool.heartbeatSucceeded(from: nodeID)
        }

        let deferred = try pool.planRAMPool(
            model: testModel(),
            estimatedStageDuration: 5,
            at: clock.now
        )
        XCTAssertNil(deferred, "must not plan while anti-flap dwell defers the epoch")

        clock.advance(5)
        let ready = try XCTUnwrap(
            try pool.planRAMPool(model: testModel(), estimatedStageDuration: 5, at: clock.now)
        )
        XCTAssertEqual(Set(ready.platforms), PlatformKind.allRequired)
    }

    /// Finding 2: suspended reconnect → active + in planningMembers.
    func testSuspendedPeerReconnectBecomesActiveAndPlans() {
        let (pool, clock) = makePool(dwell: 0)
        _ = pool.connectAllSimulatedPlatforms()
        clock.advance(1)
        _ = pool.tick(at: clock.now)

        let phoneID = try! XCTUnwrap(pool.nodeID(for: .ios))
        _ = pool.announceDrain(phoneID)
        clock.advance(1)
        let evictEvents = pool.tick(at: clock.now)
        XCTAssertTrue(evictEvents.contains {
            if case .peerEvicted(platform: .ios, nodeID: phoneID, reason: .drained) = $0 {
                return true
            }
            return false
        })
        XCTAssertEqual(pool.membership.record(for: phoneID)?.state, .suspended)
        XCTAssertFalse(pool.connectedPlatforms.contains(.ios))
        XCTAssertFalse(pool.membership.planningMembers.map(\.nodeID).contains(phoneID))

        let events = pool.connectSimulated(.ios)
        XCTAssertTrue(events.contains {
            if case .peerConnected(platform: .ios, nodeID: phoneID) = $0 { return true }
            return false
        })
        XCTAssertEqual(pool.membership.record(for: phoneID)?.state, .activeElastic)
        XCTAssertTrue(pool.membership.planningMembers.map(\.nodeID).contains(phoneID))
        XCTAssertTrue(pool.connectedPlatforms.contains(.ios))
    }

    /// Finding 3: ineligible member excluded from reported totals / plan rejected.
    func testIneligiblePeerCausesNilPlanRatherThanInflatedTotals() throws {
        let (pool, clock) = makePool(dwell: 0)

        _ = pool.connectSimulated(.windows)
        _ = pool.connectSimulated(.macos)
        _ = pool.connectSimulated(.android)

        // iOS under power duress — seat is present but StagePlanner excludes it.
        let hotPhone = CapabilityProfile(
            nodeID: NodeID("sim-ios"),
            connectivity: .elasticMobile,
            backgroundLink: .iosSuspended,
            memory: MemoryProfile(
                totalBytes: 6 * gb,
                usableBytes: 6 * gb,
                reclaimModel: .iosJetsam
            ),
            runtimeBackends: [.mlxMetal, .coreMLANE],
            power: PowerProfile(
                isWallPowered: false,
                batteryFraction: 0.05,
                thermalState: .critical
            ),
            trust: .trustedCore,
            declaresCanHostRequiredStage: true
        )
        _ = pool.connect(platform: .ios, profile: hotPhone)

        for nodeID in pool.membership.members.keys {
            pool.heartbeatSucceeded(from: nodeID)
        }
        clock.advance(1)

        XCTAssertTrue(pool.hasAllRequiredPlatforms)
        let result = try pool.planRAMPool(
            model: testModel(),
            estimatedStageDuration: 5,
            at: clock.now
        )
        XCTAssertNil(result, "must reject when an assigned set omits a required platform")
    }

    /// Finding 4: pool strongly retains the injected transport.
    func testPoolRetainsInjectedTransport() {
        weak var weakTransport: InProcessRingTransport?
        let pool: CrossPlatformPool = {
            let transport = InProcessRingTransport()
            weakTransport = transport
            return CrossPlatformPool(
                clock: TestClock(),
                config: MembershipConfig(minimumEpochDwellTime: 0),
                transport: transport
            )
        }()

        XCTAssertNotNil(weakTransport, "pool must strongly retain the injected transport")
        _ = pool.connectSimulated(.android)
        XCTAssertFalse(
            try! XCTUnwrap(weakTransport).broadcastLog.isEmpty,
            "join must still broadcast after caller dropped its transport reference"
        )
        withExtendedLifetime(pool) {}
    }

    /// Finding 5: post-tick eviction of a required platform → nil.
    func testPlanRAMPoolReturnsNilAfterTickEvictsRequiredPlatform() throws {
        let (pool, clock) = makePool(dwell: 0)
        _ = pool.connectAllSimulatedPlatforms()
        for nodeID in pool.membership.members.keys {
            pool.heartbeatSucceeded(from: nodeID)
        }
        clock.advance(1)
        _ = pool.tick(at: clock.now)

        let phoneID = try XCTUnwrap(pool.nodeID(for: .ios))
        // iOS lease max is 30s; advance past it without renewing the phone.
        clock.advance(35)
        for nodeID in pool.membership.members.keys where nodeID != phoneID {
            pool.heartbeatSucceeded(from: nodeID)
        }

        let result = try pool.planRAMPool(
            model: testModel(),
            estimatedStageDuration: 5,
            at: clock.now
        )
        XCTAssertNil(result, "must return nil after tick evicts a required platform")
        XCTAssertFalse(pool.hasAllRequiredPlatforms)
        XCTAssertEqual(pool.missingPlatforms, [.ios])
    }
}
