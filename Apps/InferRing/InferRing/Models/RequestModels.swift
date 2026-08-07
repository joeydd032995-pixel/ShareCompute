//

import Foundation
import Ring

struct Ping: Codable {
    let isAlive: Bool
}

struct ElectionMessage: Codable {
    let type: ElectionMessageType
    let candidateID: DeviceID
    let hardwareProfile: HardwareProfile
    let timestamp: Date
}

enum ElectionMessageType: Codable {
    case election
    case coordinator
}

struct ModelLoadRequest: Codable {
    let modelCard: ModelCard
    let availableFiles: [String]
    let shardMeta: ShardMetadata
    let requestID: String
    let timestamp: Date
}

struct ModelLoadResponse: Codable {
    let requestID: String
    let success: Bool
    let errorMessage: String?
    let timestamp: Date
}

struct GenerationRequest: Codable {
    let requestID: String
    let input: String
    let inputRole: ChatMessage.Role
    let history: [OpenAPIMessage]?
    let tools: [OpenAPITool]?
    let timestamp: Date
}

struct GenerationResponse: Codable {
    let requestID: String
    let success: Bool
    let errorMessage: String?
    let timestamp: Date
}

struct ChatResetRequest: Codable {
    let requestID: String
    let timestamp: Date
}

struct ChatResetResponse: Codable {
    let requestID: String
    let success: Bool
    let errorMessage: String?
    let timestamp: Date
}

struct HardwareProfileRequest: Codable {
    let timestamp: Date
}

struct HardwareProfileResponse: Codable {
    let hardwareProfile: HardwareProfile
    let timestamp: Date
}

/// Sent by a node that is about to leave — iOS backgrounding, a foreground service stopping, or a
/// user-initiated stop.
///
/// A node may only announce its *own* departure. The server checks `nodeName` against the peer
/// resolved from the connection's remote address, so a device cannot claim that some other node is
/// leaving and trick the ring into declaring itself dead. That check is the reason this endpoint
/// needs no shared secret; broader authentication of the control plane remains open work.
struct DrainRequest: Codable {
    let nodeName: String
    let requestID: String
    let timestamp: Date
}

struct DrainResponse: Codable {
    let requestID: String
    let acknowledged: Bool
    let timestamp: Date
}
