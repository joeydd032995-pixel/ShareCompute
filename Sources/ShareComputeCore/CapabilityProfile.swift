import Foundation

/// Stable identity for a participating device.
public struct NodeID: Hashable, Codable, Sendable, Comparable, CustomStringConvertible {
    public let rawValue: String

    public init(_ rawValue: String) {
        self.rawValue = rawValue
    }

    public var description: String { rawValue }

    public static func < (lhs: NodeID, rhs: NodeID) -> Bool {
        lhs.rawValue < rhs.rawValue
    }
}

/// How reliably a node holds a long-lived, bidirectional connection. Spec §3.2.
///
/// This is the single most load-bearing capability field: it decides whether a node may hold a
/// required pipeline stage, because a required stage that vanishes mid-generation stalls every
/// other rank in the group.
public enum ConnectivityClass: String, Codable, Sendable, CaseIterable {
    /// Linux / macOS / Windows: stable sockets, no OS-initiated suspension.
    case coreDesktop
    /// Android foreground service, or iOS while foregrounded — stable *while the lease holds*.
    case elasticMobile
    /// Short bursts only; assume it can disappear at any moment.
    case opportunisticMobile
}

/// What the host OS does to network connections when the app stops being frontmost. Spec §3.2.
public enum BackgroundLinkModel: String, Codable, Sendable {
    case desktopUnrestricted
    case androidForegroundService
    case iosSuspended
}

/// How the host OS reclaims memory under pressure. Spec §3.2 / §8.
public enum MemoryReclaimModel: String, Codable, Sendable {
    /// Page cache counts toward `memory.max`; mmap-heavy work can be OOM-killed.
    case linuxCgroupOOM
    /// Working sets are trimmed rather than processes killed.
    case windowsWorkingSetTrim
    /// Desktop VM behaviour; pressure causes reclaim, not arbitrary death.
    case appleUnifiedMemoryPressure
    /// iOS: the watchdog kills the process outright past its limit.
    case iosJetsam
}

public enum RuntimeBackend: String, Codable, Sendable, CaseIterable {
    case mlxMetal
    case coreMLANE
    case llamaCppCPU
    case llamaCppGPU
    case onnxRuntime
    case winmlDirectML
    case liteRT
    case webGPU
}

public enum TrustLevel: Int, Codable, Sendable, Comparable {
    case untrusted = 0
    case semiTrustedEdge = 1
    case trustedCore = 2

    public static func < (lhs: TrustLevel, rhs: TrustLevel) -> Bool {
        lhs.rawValue < rhs.rawValue
    }
}

/// A unit of pipeline work that can be placed on a node. Spec §3.2 / §9.1.
public enum StageType: String, Codable, Sendable, CaseIterable {
    case prefill
    case decodeAuthoritative
    case decodeSpeculative
    case kvStore
    case edgeOffload

    /// Required stages hold authoritative state. Losing one corrupts or stalls the request, so
    /// they may only be placed on nodes that satisfy the full eligibility rule in `StagePlanner`.
    public var isRequired: Bool {
        switch self {
        case .prefill, .decodeAuthoritative, .kvStore:
            return true
        case .decodeSpeculative, .edgeOffload:
            return false
        }
    }
}

/// Spec §3.2 `power_profile`, reduced to what is cheaply observable on every platform.
public struct PowerProfile: Codable, Sendable, Equatable {
    public enum ThermalState: Int, Codable, Sendable, Comparable {
        case nominal = 0
        case fair = 1
        case serious = 2
        case critical = 3

        public static func < (lhs: ThermalState, rhs: ThermalState) -> Bool {
            lhs.rawValue < rhs.rawValue
        }
    }

    public var isWallPowered: Bool
    /// 0...1, or nil on a device with no battery.
    public var batteryFraction: Double?
    public var thermalState: ThermalState

    public init(
        isWallPowered: Bool,
        batteryFraction: Double? = nil,
        thermalState: ThermalState = .nominal
    ) {
        self.isWallPowered = isWallPowered
        self.batteryFraction = batteryFraction
        self.thermalState = thermalState
    }

    public static let wallPowered = PowerProfile(isWallPowered: true)

    /// True when the node should shed work rather than accept more. Spec §9.3 energy-risk score,
    /// in its simplest defensible form.
    public var isUnderDuress: Bool {
        if thermalState >= .serious { return true }
        if !isWallPowered, let batteryFraction, batteryFraction < 0.2 { return true }
        return false
    }
}

public struct MemoryProfile: Codable, Sendable, Equatable {
    /// Physical memory visible to the runtime.
    public var totalBytes: Int
    /// What we are actually willing to allocate here. On iOS this is already discounted below
    /// the OS "recommended working set", because the watchdog kills at that figure in practice.
    public var usableBytes: Int
    public var reclaimModel: MemoryReclaimModel

    public init(totalBytes: Int, usableBytes: Int, reclaimModel: MemoryReclaimModel) {
        self.totalBytes = totalBytes
        self.usableBytes = usableBytes
        self.reclaimModel = reclaimModel
    }
}

/// What a device publishes at join time and refreshes on heartbeat. Spec §3.2.
///
/// Deliberately narrower than the spec's full struct: `moe_expert_capacity`,
/// `prefix_cache_capacity_gb`, quantization negotiation and intermediate-compression modes are
/// omitted because nothing in this milestone reads them, and unread fields drift out of sync
/// with reality. They can be added when the code that consumes them exists.
public struct CapabilityProfile: Codable, Sendable, Equatable, Identifiable {
    public var id: NodeID { nodeID }

    public var nodeID: NodeID
    public var connectivity: ConnectivityClass
    public var backgroundLink: BackgroundLinkModel
    public var memory: MemoryProfile
    public var runtimeBackends: [RuntimeBackend]
    public var power: PowerProfile
    public var trust: TrustLevel
    public var dialectVersion: String

    /// Operator/user consent for this node to hold required stages. This is a *declaration*, not
    /// the eligibility decision — `StagePlanner` combines it with connectivity, trust and lease
    /// headroom. A node may always decline; it may not unilaterally promote itself.
    public var declaresCanHostRequiredStage: Bool

    public init(
        nodeID: NodeID,
        connectivity: ConnectivityClass,
        backgroundLink: BackgroundLinkModel,
        memory: MemoryProfile,
        runtimeBackends: [RuntimeBackend],
        power: PowerProfile,
        trust: TrustLevel,
        dialectVersion: String = CapabilityProfile.currentDialectVersion,
        declaresCanHostRequiredStage: Bool
    ) {
        self.nodeID = nodeID
        self.connectivity = connectivity
        self.backgroundLink = backgroundLink
        self.memory = memory
        self.runtimeBackends = runtimeBackends
        self.power = power
        self.trust = trust
        self.dialectVersion = dialectVersion
        self.declaresCanHostRequiredStage = declaresCanHostRequiredStage
    }

    public static let currentDialectVersion = "0.1.0"

    /// Lease length this node's OS can actually honour, before any policy is applied.
    ///
    /// iOS gets a deliberately short lease: `willResignActive` gives only a brief window to drain,
    /// so the scheduler must never hand it work it cannot finish inside one lease period.
    public var maximumSupportableLeaseDuration: TimeInterval {
        switch backgroundLink {
        case .desktopUnrestricted: return 300
        case .androidForegroundService: return 120
        case .iosSuspended: return 30
        }
    }
}
