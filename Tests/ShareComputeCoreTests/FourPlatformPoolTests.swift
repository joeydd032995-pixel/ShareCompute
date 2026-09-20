import Foundation
@testable import ShareComputeCore
import Testing

@Suite("Four-platform pool connect")
struct FourPlatformPoolTests {

    @Test("simulated Windows/macOS/iOS/Android all connect")
    func allFourConnect() {
        let clock = TestClock()
        let pool = CrossPlatformPool(
            clock: clock,
            config: MembershipConfig(minimumEpochDwellTime: 0)
        )

        let events = pool.connectAllSimulatedPlatforms()

        #expect(pool.connectedPlatforms == PlatformKind.allRequired)
        #expect(pool.missingPlatforms.isEmpty)
        #expect(pool.hasAllRequiredPlatforms)
        #expect(events.contains(.allRequiredPlatformsConnected(PlatformKind.allRequired)))

        for platform in PlatformKind.allCases {
            #expect(pool.nodeID(for: platform) != nil)
        }
    }

    @Test("RAM pool plan covers every platform seat")
    func ramPoolPlan() throws {
        let clock = TestClock()
        let pool = CrossPlatformPool(
            clock: clock,
            config: MembershipConfig(minimumEpochDwellTime: 0)
        )
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

        let result = try #require(try pool.planRAMPool(model: model, estimatedStageDuration: 5, at: clock.now))

        #expect(Set(result.platforms) == PlatformKind.allRequired)
        #expect(result.plan.worldSize == 4)
        #expect(result.plan.assignments.map(\.layerCount).reduce(0, +) == 32)
        #expect(result.plan.assignments.first?.startLayer == 0)
        #expect(result.plan.assignments.last?.endLayer == 32)
    }

    @Test("missing a platform blocks the pool plan")
    func missingPlatformBlocksPlan() throws {
        let clock = TestClock()
        let pool = CrossPlatformPool(
            clock: clock,
            config: MembershipConfig(minimumEpochDwellTime: 0)
        )

        _ = pool.connectSimulated(.windows)
        _ = pool.connectSimulated(.macos)
        _ = pool.connectSimulated(.ios)
        // Android deliberately absent

        #expect(pool.missingPlatforms == [.android])
        #expect(!pool.hasAllRequiredPlatforms)

        let model = ModelPlacementSpec(
            modelID: "test",
            layerCount: 16,
            totalWeightBytes: 4 * gb,
            perNodeOverheadBytes: 64 * 1024 * 1024
        )
        let result = try pool.planRAMPool(model: model, at: clock.now)
        #expect(result == nil)
    }

    @Test("in-process transport records joins")
    func transportRecordsJoins() {
        let clock = TestClock()
        let transport = InProcessRingTransport()
        let pool = CrossPlatformPool(
            clock: clock,
            config: MembershipConfig(minimumEpochDwellTime: 0),
            transport: transport
        )

        _ = pool.connectSimulated(.android)
        #expect(!transport.broadcastLog.isEmpty)
        if case let .join(platform, profile) = transport.broadcastLog.first {
            #expect(platform == .android)
            #expect(profile.nodeID == NodeID("sim-android"))
        } else {
            Issue.record("expected join broadcast")
        }
    }

    @Test("SimulatedPlatformPeer profiles match platform capabilities")
    func simulatedProfiles() {
        let windows = SimulatedPlatformPeer.profile(for: .windows)
        #expect(windows.connectivity == .coreDesktop)
        #expect(windows.memory.reclaimModel == .windowsWorkingSetTrim)
        #expect(windows.runtimeBackends.contains(.winmlDirectML))

        let android = SimulatedPlatformPeer.profile(for: .android)
        #expect(android.connectivity == .elasticMobile)
        #expect(android.backgroundLink == .androidForegroundService)
        #expect(android.runtimeBackends.contains(.liteRT))

        let ios = SimulatedPlatformPeer.profile(for: .ios)
        #expect(ios.backgroundLink == .iosSuspended)
        #expect(ios.maximumSupportableLeaseDuration == 30)

        let macos = SimulatedPlatformPeer.profile(for: .macos)
        #expect(macos.connectivity == .coreDesktop)
        #expect(macos.runtimeBackends.contains(.mlxMetal))
    }
}
