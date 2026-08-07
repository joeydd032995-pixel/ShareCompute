import Foundation
@testable import ShareComputeCore

let gb = 1024 * 1024 * 1024

extension CapabilityProfile {

    /// A wall-powered Mac: the archetypal core-ring node.
    static func mac(
        _ name: String = "mac",
        usableGB: Int = 32,
        trust: TrustLevel = .trustedCore,
        power: PowerProfile = .wallPowered,
        declaresCanHostRequiredStage: Bool = true
    ) -> CapabilityProfile {
        CapabilityProfile(
            nodeID: NodeID(name),
            connectivity: .coreDesktop,
            backgroundLink: .desktopUnrestricted,
            memory: MemoryProfile(
                totalBytes: usableGB * gb,
                usableBytes: usableGB * gb,
                reclaimModel: .appleUnifiedMemoryPressure
            ),
            runtimeBackends: [.mlxMetal],
            power: power,
            trust: trust,
            declaresCanHostRequiredStage: declaresCanHostRequiredStage
        )
    }

    /// An iPhone: elastic, battery-powered, killed by the watchdog rather than reclaimed.
    static func iPhone(
        _ name: String = "iphone",
        usableGB: Int = 6,
        trust: TrustLevel = .trustedCore,
        power: PowerProfile = PowerProfile(isWallPowered: false, batteryFraction: 0.8),
        declaresCanHostRequiredStage: Bool = true
    ) -> CapabilityProfile {
        CapabilityProfile(
            nodeID: NodeID(name),
            connectivity: .elasticMobile,
            backgroundLink: .iosSuspended,
            memory: MemoryProfile(
                totalBytes: usableGB * gb,
                usableBytes: usableGB * gb,
                reclaimModel: .iosJetsam
            ),
            runtimeBackends: [.mlxMetal],
            power: power,
            trust: trust,
            declaresCanHostRequiredStage: declaresCanHostRequiredStage
        )
    }

    /// A node we explicitly do not depend on.
    static func opportunistic(
        _ name: String = "tablet",
        usableGB: Int = 4,
        trust: TrustLevel = .semiTrustedEdge
    ) -> CapabilityProfile {
        CapabilityProfile(
            nodeID: NodeID(name),
            connectivity: .opportunisticMobile,
            backgroundLink: .iosSuspended,
            memory: MemoryProfile(
                totalBytes: usableGB * gb,
                usableBytes: usableGB * gb,
                reclaimModel: .iosJetsam
            ),
            runtimeBackends: [.mlxMetal],
            power: PowerProfile(isWallPowered: false, batteryFraction: 0.5),
            trust: trust,
            declaresCanHostRequiredStage: false
        )
    }
}

/// Deterministic clock so lease and heartbeat behaviour is tested by advancing time explicitly
/// rather than by sleeping.
final class TestClock: RingClock, @unchecked Sendable {
    private var current: Date

    init(_ start: Date = Date(timeIntervalSince1970: 1_000_000)) {
        self.current = start
    }

    var now: Date { current }

    func advance(_ interval: TimeInterval) {
        current = current.addingTimeInterval(interval)
    }
}
