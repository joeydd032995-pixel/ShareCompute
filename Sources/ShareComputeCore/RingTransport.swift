import Foundation

/// Control-plane messages exchanged between a pool hub and its peers.
///
/// Deliberately narrow: join, heartbeat, drain, and epoch notification. Model weights and
/// token streams stay on the runtime adapters (MLX today; llama.cpp / ONNX / WinML later).
public enum PoolWireMessage: Codable, Sendable, Equatable {
    case join(platform: PlatformKind, profile: CapabilityProfile)
    case heartbeat(NodeID)
    case drain(NodeID)
    case epochChanged(Epoch, members: [NodeID])
    case acknowledged(NodeID)
}

/// Host-provided transport. The core never opens sockets — TCP, WebSocket, Bonjour, or an
/// in-process queue are all adapter concerns. This keeps ShareComputeCore buildable on Linux
/// and Windows without NIO or UIKit, matching the Package.swift contract.
public protocol RingTransport: AnyObject {
    /// Deliver a control-plane message to one peer.
    func send(to nodeID: NodeID, message: PoolWireMessage) throws

    /// Broadcast to every currently connected peer.
    func broadcast(_ message: PoolWireMessage) throws
}

/// In-process transport used by simulated peers and unit tests. No network, no sleeping.
public final class InProcessRingTransport: RingTransport {
    public private(set) var inbox: [NodeID: [PoolWireMessage]] = [:]
    public private(set) var broadcastLog: [PoolWireMessage] = []

    public init() {}

    public func send(to nodeID: NodeID, message: PoolWireMessage) throws {
        inbox[nodeID, default: []].append(message)
    }

    public func broadcast(_ message: PoolWireMessage) throws {
        broadcastLog.append(message)
        for nodeID in inbox.keys {
            inbox[nodeID, default: []].append(message)
        }
    }

    public func takeInbox(for nodeID: NodeID) -> [PoolWireMessage] {
        let messages = inbox[nodeID] ?? []
        inbox[nodeID] = []
        return messages
    }
}
