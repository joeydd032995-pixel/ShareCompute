import Foundation
import ShareComputeCore

/// Runnable demo: connect simulated Windows, macOS, iOS, and Android peers to one pool,
/// advance past anti-flap dwell, and print a RAM-pooled shard plan.
///
/// In-process by default (no sockets). Pass `--fail-platform <name>` to omit a seat and
/// exit non-zero — proving the pool refuses to plan without all four platforms.
///
/// For the **networked multi-process** path that joins over localhost TCP (closer to
/// real devices), use the Python demo instead:
///
///     python3 scripts/four_platform_pool_demo.py
///     python3 scripts/four_platform_pool_demo.py --fail-platform android
///
///     swift run FourPlatformDemo
///     swift run FourPlatformDemo -- --fail-platform android
enum DemoFailure: Error, CustomStringConvertible {
    case planUnavailable
    case missingPlatforms(Set<PlatformKind>)

    var description: String {
        switch self {
        case .planUnavailable:
            return "pool did not produce a plan"
        case let .missingPlatforms(missing):
            let names = missing.sorted().map(\.displayName).joined(separator: ", ")
            return "required platform(s) did not connect: \(names)"
        }
    }
}

@main
enum FourPlatformDemo {
    static func main() throws {
        let failPlatforms = parseFailPlatforms(CommandLine.arguments)

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
        print("Mode: in-process simulated peers (TCP multi-process → Python demo)")
        if !failPlatforms.isEmpty {
            let names = failPlatforms.sorted().map(\.displayName).joined(separator: ", ")
            print("Negative test: omitting \(names)")
        }
        print("Connecting simulated peers…\n")

        for platform in PlatformKind.allCases.sorted() {
            if failPlatforms.contains(platform) {
                print("  · skipping \(platform.displayName) (--fail-platform)")
                continue
            }
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

        if !pool.hasAllRequiredPlatforms {
            let missing = pool.missingPlatforms
            print("\nFAIL: platform(s) did not connect:")
            for platform in missing.sorted() {
                print("  ✗ \(platform.displayName)")
            }
            throw DemoFailure.missingPlatforms(missing)
        }

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
        print("(In-process simulations here; networked multi-process TCP → python3 scripts/four_platform_pool_demo.py)")
        print("See docs/FOUR-PLATFORM-CONNECT.md")
    }

    /// Parse `--fail-platform <name>` (repeatable) from argv.
    static func parseFailPlatforms(_ args: [String]) -> Set<PlatformKind> {
        var result: Set<PlatformKind> = []
        var index = 1
        while index < args.count {
            let arg = args[index]
            if arg == "--fail-platform", index + 1 < args.count {
                let raw = args[index + 1].lowercased()
                if let platform = PlatformKind(rawValue: raw) {
                    result.insert(platform)
                } else {
                    // Avoid C `stderr` — not concurrency-safe under Swift 6 on Linux.
                    let warning = Data("warning: unknown platform '\(args[index + 1])'\n".utf8)
                    try? FileHandle.standardError.write(contentsOf: warning)
                }
                index += 2
                continue
            }
            if arg.hasPrefix("--fail-platform=") {
                let raw = String(arg.dropFirst("--fail-platform=".count)).lowercased()
                if let platform = PlatformKind(rawValue: raw) {
                    result.insert(platform)
                }
            }
            index += 1
        }
        return result
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
