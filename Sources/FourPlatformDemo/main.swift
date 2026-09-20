import Foundation
import ShareComputeCore

/// Runnable demo: connect simulated Windows, macOS, iOS, and Android peers to one pool,
/// advance past anti-flap dwell, and print a RAM-pooled shard plan.
///
///     swift run FourPlatformDemo
enum DemoFailure: Error, CustomStringConvertible {
    case planUnavailable
    var description: String { "pool did not produce a plan" }
}

@main
enum FourPlatformDemo {
    static func main() throws {
        let clock = DemoClock()
        let transport = InProcessRingTransport()
        let pool = CrossPlatformPool(
            clock: clock,
            config: MembershipConfig(
                leaseDuration: 60,
                heartbeatInterval: 2,
                missedHeartbeatsBeforeEviction: 3,
                minimumEpochDwellTime: 0  // demos want an epoch immediately
            ),
            transport: transport
        )

        print("ShareCompute four-platform pool demo")
        print("====================================")
        print("Connecting simulated peers…\n")

        for platform in PlatformKind.allCases.sorted() {
            let events = pool.connectSimulated(platform)
            for event in events {
                switch event {
                case let .peerConnected(platform, nodeID):
                    let profile = pool.membership.record(for: nodeID)!.profile
                    let gb = Double(profile.memory.usableBytes) / (1024 * 1024 * 1024)
                    let gbLabel = String(format: "%.0f", gb)
                    print("  ✓ \(platform.displayName) connected as \(nodeID) (\(gbLabel) GB usable)")
                case .allRequiredPlatformsConnected:
                    print("\n  All four platforms are in the pool.\n")
                default:
                    break
                }
            }
        }

        precondition(pool.hasAllRequiredPlatforms, "demo requires Windows/macOS/iOS/Android")

        // Heartbeats so leases renew; tick so epoch advances with dwell=0.
        for nodeID in pool.membership.members.keys {
            pool.heartbeatSucceeded(from: nodeID)
        }
        _ = pool.tick(at: clock.now)

        let model = ModelPlacementSpec(
            modelID: "demo-48L",
            layerCount: 48,
            totalWeightBytes: 24 * 1024 * 1024 * 1024, // 24 GB weights
            perNodeOverheadBytes: 512 * 1024 * 1024
        )

        guard let result = try pool.planRAMPool(model: model, estimatedStageDuration: 5, at: clock.now) else {
            throw DemoFailure.planUnavailable
        }

        let totalGB = Double(result.totalUsableBytes) / (1024 * 1024 * 1024)
        let totalLabel = String(format: "%.1f", totalGB)
        let platformList = result.platforms.map(\.displayName).joined(separator: ", ")
        print("RAM pool ready — \(totalLabel) GB across \(platformList)")
        print("Epoch \(result.plan.epoch.value)  model \(result.plan.modelID)  layers \(result.plan.layerCount)\n")
        print("Shard plan:")
        for assignment in result.plan.assignments {
            let platform = pool.platform(for: assignment.nodeID)?.displayName ?? "?"
            let gb = Double(assignment.estimatedBytes) / (1024 * 1024 * 1024)
            let gbLabel = String(format: "%.2f", gb)
            let padded = platform.padding(toLength: 7, withPad: " ", startingAt: 0)
            print(
                "  rank \(assignment.rank)  \(padded)  \(assignment.nodeID)  "
                + "layers [\(assignment.startLayer),\(assignment.endLayer))  ~\(gbLabel) GB"
            )
        }

        print("\nSuccess: Windows, macOS, iOS, and Android are connected and pooled.")
        print("(Peers in this demo are in-process simulations — see docs/FOUR-PLATFORM-CONNECT.md)")
    }
}

/// Deterministic clock for the executable demo (no sleeps).
final class DemoClock: RingClock, @unchecked Sendable {
    private var current = Date(timeIntervalSince1970: 1_700_000_000)
    var now: Date { current }
    func advance(_ seconds: TimeInterval) {
        current = current.addingTimeInterval(seconds)
    }
}
