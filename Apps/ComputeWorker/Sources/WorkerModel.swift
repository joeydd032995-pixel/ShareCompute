import Foundation
import Network
import Security
import CryptoKit
import UIKit

private struct Pairing: Decodable {
    let host: String
    let port: UInt16
    let pin: String
    let runtime: String
    let node: String
    let token: String
    let budget_mib: UInt64
}
private enum WorkerError: Error { case invalidPairing, connectionClosed, protocolError, wrongCertificate }

// One reader and one writer per connection; NWConnection owns its network queue.
private final class Stream: @unchecked Sendable {
    let connection: NWConnection
    private var buffered = Data()
    private static let queue = DispatchQueue(label: "ShareCompute.network", attributes: .concurrent)

    init(host: String, port: UInt16, pin: String? = nil) {
        let tcp = NWProtocolTCP.Options(); tcp.noDelay = true; tcp.connectionTimeout = 10
        let parameters: NWParameters
        if let pin = pin {
            let tls = NWProtocolTLS.Options()
            sec_protocol_options_set_min_tls_protocol_version(tls.securityProtocolOptions, .TLSv12)
            sec_protocol_options_set_verify_block(tls.securityProtocolOptions, { _, trust, complete in
                let secTrust = sec_trust_copy_ref(trust).takeRetainedValue()
                guard let certificate = SecTrustGetCertificateAtIndex(secTrust, 0) else { complete(false); return }
                let der = SecCertificateCopyData(certificate) as Data
                let actual = SHA256.hash(data: der).map { String(format: "%02x", $0) }.joined()
                complete(actual == pin)
            }, Self.queue)
            parameters = NWParameters(tls: tls, tcp: tcp)
        } else { parameters = NWParameters(tls: nil, tcp: tcp) }
        connection = NWConnection(host: NWEndpoint.Host(host), port: NWEndpoint.Port(rawValue: port)!, using: parameters)
    }

    func connect() async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            // state handlers for this connection run serially on this dedicated queue.
            let queue = DispatchQueue(label: "ShareCompute.connect.\(UUID().uuidString)")
            var finished = false
            connection.stateUpdateHandler = { state in
                guard !finished else { return }
                switch state {
                case .ready: finished = true; continuation.resume()
                case .failed(let error): finished = true; continuation.resume(throwing: error)
                case .cancelled: finished = true; continuation.resume(throwing: WorkerError.connectionClosed)
                default: break
                }
            }
            connection.start(queue: queue)
            queue.asyncAfter(deadline: .now() + 10) { [weak self] in
                if !finished { self?.connection.cancel() }
            }
        }
    }
    func write(_ data: Data) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            connection.send(content: data, completion: .contentProcessed { error in
                if let error { continuation.resume(throwing: error) } else { continuation.resume() }
            })
        }
    }
    func read() async throws -> Data? {
        if !buffered.isEmpty { let data = buffered; buffered.removeAll(keepingCapacity: true); return data }
        return try await withCheckedThrowingContinuation { continuation in
            connection.receive(minimumIncompleteLength: 1, maximumLength: 65536) { data, _, complete, error in
                if let error { continuation.resume(throwing: error) }
                else if let data, !data.isEmpty { continuation.resume(returning: data) }
                else if complete { continuation.resume(returning: nil) }
                else { continuation.resume(throwing: WorkerError.connectionClosed) }
            }
        }
    }
    func sendJSON(_ object: [String: Any]) async throws {
        var data = try JSONSerialization.data(withJSONObject: object); data.append(10); try await write(data)
    }
    func receiveJSON() async throws -> [String: Any] {
        var line = Data()
        while true {
            guard let data = try await read() else { throw WorkerError.connectionClosed }
            line.append(data)
            if let index = line.firstIndex(of: 10) {
                buffered = Data(line[line.index(after: index)...]); let message = Data(line[..<index])
                guard message.count <= 8192, let value = try JSONSerialization.jsonObject(with: message) as? [String: Any] else {
                    throw WorkerError.protocolError
                }
                return value
            }
            if line.count > 8192 { throw WorkerError.protocolError }
        }
    }
    func close() { connection.cancel() }
}

@MainActor
final class WorkerModel: ObservableObject {
    @Published var pairingText = ""
    @Published var status = "Import the pairing file created on your laptop."
    @Published var running = false
    @Published var allocated: UInt64 = 0
    @Published var peak: UInt64 = 0
    @Published var graphs: UInt64 = 0
    private var runner: Task<Void, Never>?
    private var heartbeat: Task<Void, Never>?
    private var channels: [String: Task<Void, Never>] = [:]
    private var streams: [UUID: Stream] = [:]
    private var nativePort: UInt16?
    private var nativeBudget: UInt64?
    private var epoch = UUID()

    func startSimulatorIfRequested() {
        #if targetEnvironment(simulator)
        if !running, let encoded = ProcessInfo.processInfo.environment["SC_PAIRING_B64"],
           let data = Data(base64Encoded: encoded), let text = String(data: data, encoding: .utf8) {
            pairingText = text; start()
        }
        #endif
    }
    func start() {
        guard !running else { return }
        do {
            let pair = try JSONDecoder().decode(Pairing.self, from: Data(pairingText.utf8))
            guard pair.node == "iphone", pair.port > 0, pair.runtime == runtimeRevision,
                  pair.runtime == String(cString: sc_worker_revision()), pair.pin.count == 64,
                  pair.pin.allSatisfy({ $0.isHexDigit && !$0.isUppercase }), pair.token.count == 64,
                  (64...4096).contains(pair.budget_mib) else { throw WorkerError.invalidPairing }
            if let nativeBudget, nativeBudget != pair.budget_mib {
                status = "Restart the app to change its native memory budget."; return
            }
            if nativePort == nil {
                let port = UInt16.random(in: 20000...30000); nativePort = port; nativeBudget = pair.budget_mib
                Thread.detachNewThread {
                    let result = sc_worker_run(Int32(port), pair.budget_mib * 1_048_576, 2)
                    Task { @MainActor [weak self] in self?.stop("Native worker exited (\(result)); restart the app") }
                }
            }
            running = true; epoch = UUID(); let current = epoch
            UIApplication.shared.isIdleTimerDisabled = true; status = "Connecting…"
            runner = Task { [weak self] in
                guard let self else { return }
                do { try await self.run(pair, current: current) }
                catch { if self.epoch == current { self.stop("Disconnected: \(error.localizedDescription)") } }
            }
        } catch { status = "Invalid pairing file: \(error.localizedDescription)" }
    }

    func stop(_ message: String) {
        print("SC_IOS \(message)")
        epoch = UUID(); running = false; status = message
        runner?.cancel(); heartbeat?.cancel()
        for task in channels.values { task.cancel() }
        for stream in streams.values { stream.close() }
        streams.removeAll(); channels.removeAll()
        UIApplication.shared.isIdleTimerDisabled = false
    }
    private func registered(_ stream: Stream) -> UUID { let id = UUID(); streams[id] = stream; return id }

    private func run(_ pair: Pairing, current: UUID) async throws {
        guard let port = nativePort else { throw WorkerError.protocolError }
        // Listener creation happens on the native thread; retry local readiness briefly.
        var ready = false
        for _ in 0..<40 {
            try Task.checkCancellation()
            let probe = Stream(host: "127.0.0.1", port: port)
            do { try await probe.connect(); ready = true } catch { }
            probe.close()
            if ready { break }; try await Task.sleep(nanoseconds: 50_000_000)
        }
        guard ready else { throw WorkerError.connectionClosed }
        let control = Stream(host: pair.host, port: pair.port, pin: pair.pin); let id = registered(control)
        defer { control.close(); streams.removeValue(forKey: id) }
        try await control.connect(); try Task.checkCancellation()
        #if targetEnvironment(simulator)
        let simulator = true
        #else
        let simulator = false
        #endif
        try await control.sendJSON(["kind": "control", "node": pair.node, "token": pair.token,
                                    "runtime": pair.runtime, "platform": "ios", "simulator": simulator,
                                    "budget_mib": pair.budget_mib, "session": UUID().uuidString])
        let ack = try await control.receiveJSON()
        guard ack["ok"] as? Bool == true else { throw WorkerError.protocolError }
        status = "Connected — waiting for model layers"
        print("SC_IOS connected; native worker ready")
        heartbeat = Task { [weak self] in
            guard let self else { return }
            do {
                while !Task.isCancelled {
                    self.allocated = sc_worker_allocated(); self.peak = sc_worker_peak(); self.graphs = sc_worker_graphs()
                    try await control.sendJSON(["op": "stats", "allocated_bytes": self.allocated,
                                                "peak_bytes": self.peak, "graph_calls": self.graphs])
                    try await Task.sleep(nanoseconds: 500_000_000)
                }
            } catch { if self.epoch == current && !Task.isCancelled { self.stop("Heartbeat failed") } }
        }
        while !Task.isCancelled {
            let message = try await control.receiveJSON()
            guard message["op"] as? String == "open", let channel = message["channel"] as? String,
                  channel.count == 32, channels.count < 4, channels[channel] == nil else { throw WorkerError.protocolError }
            channels[channel] = Task { [weak self] in
                guard let self else { return }
                defer { self.channels.removeValue(forKey: channel) }
                do { try await self.tunnel(pair, port: port, channel: channel) }
                catch { if self.epoch == current && !Task.isCancelled { self.stop("Compute tunnel failed: \(error.localizedDescription)") } }
            }
        }
    }
    private func tunnel(_ pair: Pairing, port: UInt16, channel: String) async throws {
        let remote = Stream(host: pair.host, port: pair.port, pin: pair.pin)
        let local = Stream(host: "127.0.0.1", port: port)
        let remoteID = registered(remote); let localID = registered(local)
        defer { remote.close(); local.close(); streams.removeValue(forKey: remoteID); streams.removeValue(forKey: localID) }
        try await remote.connect()
        try await remote.sendJSON(["kind": "data", "node": pair.node, "token": pair.token, "channel": channel])
        let ack = try await remote.receiveJSON()
        guard ack["ok"] as? Bool == true else { throw WorkerError.protocolError }
        try await local.connect()
        try await withThrowingTaskGroup(of: Void.self) { group in
            group.addTask { while let data = try await remote.read() { try await local.write(data) } }
            group.addTask { while let data = try await local.read() { try await remote.write(data) } }
            do { try await group.next() }
            catch { remote.close(); local.close(); group.cancelAll(); throw error }
            remote.close(); local.close(); group.cancelAll()
            // Cancellation closes the opposite receive; its cancellation error is expected after EOF.
            while !group.isEmpty { _ = try? await group.next() }
        }
    }
}
