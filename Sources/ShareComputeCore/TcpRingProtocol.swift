import Foundation

/// Framing helpers for the localhost TCP hub protocol used by
/// `scripts/four_platform_pool_demo.py`.
///
/// ShareComputeCore still does not open sockets — that stays a host / demo
/// concern so the package remains free of NIO and macOS-only Network APIs.
/// This type documents the wire shape and can encode/decode newline-delimited
/// JSON join messages for adapters that *do* own a TCP socket.
public enum TcpRingProtocol {
    public static let version = 1

    /// Narrow join payload mirrored by the Python multi-process demo.
    public struct JoinRequest: Codable, Sendable, Equatable {
        public var v: Int
        public var type: String
        public var platform: PlatformKind
        public var nodeID: String
        public var usableGB: Double

        public init(platform: PlatformKind, nodeID: String, usableGB: Double) {
            self.v = TcpRingProtocol.version
            self.type = "join"
            self.platform = platform
            self.nodeID = nodeID
            self.usableGB = usableGB
        }

        enum CodingKeys: String, CodingKey {
            case v, type, platform
            case nodeID = "node_id"
            case usableGB = "usable_gb"
        }
    }

    public struct Ack: Codable, Sendable, Equatable {
        public var v: Int
        public var type: String
        public var nodeID: String
        public var platform: PlatformKind

        enum CodingKeys: String, CodingKey {
            case v, type, platform
            case nodeID = "node_id"
        }
    }

    /// Encode a join line (including trailing newline) matching the Python demo.
    public static func encodeJoinLine(
        platform: PlatformKind,
        nodeID: String,
        usableGB: Double
    ) throws -> Data {
        let req = JoinRequest(platform: platform, nodeID: nodeID, usableGB: usableGB)
        var data = try JSONEncoder().encode(req)
        data.append(contentsOf: [0x0A]) // \n
        return data
    }

    /// Decode one JSON object from a single TCP line (newline already stripped).
    public static func decodeJoinLine(_ line: Data) throws -> JoinRequest {
        try JSONDecoder().decode(JoinRequest.self, from: line)
    }
}

/// Scaffold for a future host-owned TCP `RingTransport`.
///
/// Not used by unit tests (they keep `InProcessRingTransport`). The runnable
/// multi-process connect path that can fail on missing peers lives in
/// `scripts/four_platform_pool_demo.py`.
public final class TcpRingTransport: RingTransport {
    public private(set) var sent: [(NodeID, PoolWireMessage)] = []
    public private(set) var broadcasts: [PoolWireMessage] = []

    /// When false, `send` / `broadcast` throw — used to model "peer never connected".
    public var isConnected: Bool

    public enum TransportError: Error, CustomStringConvertible {
        case notConnected
        public var description: String { "TcpRingTransport is not connected" }
    }

    public init(isConnected: Bool = false) {
        self.isConnected = isConnected
    }

    public func send(to nodeID: NodeID, message: PoolWireMessage) throws {
        guard isConnected else { throw TransportError.notConnected }
        sent.append((nodeID, message))
    }

    public func broadcast(_ message: PoolWireMessage) throws {
        guard isConnected else { throw TransportError.notConnected }
        broadcasts.append(message)
    }
}
