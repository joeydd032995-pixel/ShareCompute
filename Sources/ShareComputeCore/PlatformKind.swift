import Foundation

/// Host operating system family a pool peer claims to represent.
///
/// ShareCompute's membership core is platform-neutral; InferRing today only runs on Apple
/// silicon. This enum is the explicit label used by the cross-platform pool so Windows,
/// macOS, iOS and Android peers can join the *same* membership epoch — including in-process
/// simulated stand-ins used by the four-platform demo.
public enum PlatformKind: String, Codable, Sendable, CaseIterable, Comparable {
    case windows
    case macos
    case ios
    case android

    public static func < (lhs: PlatformKind, rhs: PlatformKind) -> Bool {
        lhs.rawValue < rhs.rawValue
    }

    /// The four platforms the pool must see before a multi-platform connect is considered complete.
    public static let allRequired: Set<PlatformKind> = Set(PlatformKind.allCases)

    public var displayName: String {
        switch self {
        case .windows: return "Windows"
        case .macos: return "macOS"
        case .ios: return "iOS"
        case .android: return "Android"
        }
    }
}
