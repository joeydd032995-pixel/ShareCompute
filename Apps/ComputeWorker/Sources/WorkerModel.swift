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
private enum WorkerError: LocalizedError {
    case invalidPairing, connectionClosed, protocolError, wrongCertificate
    case nativeNotListening, rejectedByLaptop
    case unreachable(endpoint: String, reason: String)

    /// Names the case without its associated values, for the event log.
    var logName: String {
        switch self {
        case .invalidPairing: return "invalidPairing"
        case .connectionClosed: return "connectionClosed"
        case .protocolError: return "protocolError"
        case .wrongCertificate: return "wrongCertificate"
        case .nativeNotListening: return "nativeNotListening"
        case .rejectedByLaptop: return "rejectedByLaptop"
        case .unreachable: return "unreachable"
        }
    }

    var errorDescription: String? {
        switch self {
        case .invalidPairing: return "This is not an iPhone pairing code for this app version."
        case .connectionClosed: return "The laptop closed the connection. Check the laptop dashboard for the test result."
        case .protocolError: return "The laptop sent an unexpected message."
        case .wrongCertificate: return "The laptop certificate does not match the scanned code. Scan the iPhone QR again."
        case .nativeNotListening: return "The native compute worker did not start. Force-quit and reopen the app."
        case .rejectedByLaptop:
            return "The laptop refused this iPhone. Scan the iPhone QR from the test that is running now; the laptop log names the reason."
        case .unreachable(let endpoint, let reason):
            return "Could not reach the laptop at \(endpoint) (\(reason)). Turn on Settings › Privacy & Security › Local Network › ShareCompute Worker, use the same Wi-Fi as the laptop without a VPN, and allow the laptop's firewall prompt."
        }
    }
}

// Set from the TLS verify queue, read from the connection's state queue.
private final class PinCheck: @unchecked Sendable {
    private let lock = NSLock()
    private var mismatched = false
    var rejected: Bool { lock.lock(); defer { lock.unlock() }; return mismatched }
    func reject() { lock.lock(); mismatched = true; lock.unlock() }
}

// NWError is not a LocalizedError, so localizedDescription can hide the POSIX, DNS or TLS cause.
private func describe(_ error: NWError) -> String { error.debugDescription }

/// The error as the log records it: its type, and the most specific text available. The operator
/// message (`localizedDescription`) can be as vague as "WorkerError error 1"; this is for diagnosis.
private func errorFields(_ error: Error) -> LogFields {
    switch error {
    case let worker as WorkerError:
        return ["error_type": .text("WorkerError.\(worker.logName)"), "error": .text(worker.errorDescription)]
    case let network as NWError:
        return ["error_type": "NWError", "error": .text(describe(network))]
    case is CancellationError:
        return ["error_type": "CancellationError", "error": "cancelled"]
    default:
        let bridged = error as NSError
        return ["error_type": .text(String(describing: type(of: error))),
                "error": .text("\(bridged.domain) \(bridged.code): \(error.localizedDescription)")]
    }
}

/// Counts bytes from two tunnel tasks that run concurrently; the lock is the whole synchronisation.
private final class ByteCount: @unchecked Sendable {
    private let lock = NSLock()
    private var total = 0
    func add(_ count: Int) { lock.lock(); total += count; lock.unlock() }
    var value: Int { lock.lock(); defer { lock.unlock() }; return total }
}

/// Records which half of a tunnel finished first, for the log. The first writer wins.
private final class FirstReason: @unchecked Sendable {
    private let lock = NSLock()
    private var text: String?
    func set(_ reason: String) { lock.lock(); if text == nil { text = reason }; lock.unlock() }
    var value: String? { lock.lock(); defer { lock.unlock() }; return text }
}

// One reader and one writer per connection; NWConnection owns its network queue.
private final class Stream: @unchecked Sendable {
    let connection: NWConnection
    private let endpoint: String
    private let pinCheck: PinCheck
    /// Names this stream in the log ("control", "channel-laptop"). nil stays silent: the local
    /// readiness probe makes up to 40 attempts and would only be noise.
    private let label: String?
    private var buffered = Data()
    private static let queue = DispatchQueue(label: "ShareCompute.network", attributes: .concurrent)

    init(host: String, port: UInt16, pin: String? = nil, label: String? = nil) {
        self.label = label
        let tcp = NWProtocolTCP.Options(); tcp.noDelay = true; tcp.connectionTimeout = 10
        let parameters: NWParameters
        let pinCheck = PinCheck(); self.pinCheck = pinCheck
        if let pin = pin {
            let tls = NWProtocolTLS.Options()
            sec_protocol_options_set_min_tls_protocol_version(tls.securityProtocolOptions, .TLSv12)
            sec_protocol_options_set_verify_block(tls.securityProtocolOptions, { _, trust, complete in
                let secTrust = sec_trust_copy_ref(trust).takeRetainedValue()
                guard let certificate = SecTrustGetCertificateAtIndex(secTrust, 0) else { pinCheck.reject(); complete(false); return }
                let der = SecCertificateCopyData(certificate) as Data
                let actual = SHA256.hash(data: der).map { String(format: "%02x", $0) }.joined()
                if actual != pin { pinCheck.reject() }
                complete(actual == pin)
            }, Self.queue)
            parameters = NWParameters(tls: tls, tcp: tcp)
        } else { parameters = NWParameters(tls: nil, tcp: tcp) }
        endpoint = "\(host):\(port)"
        connection = NWConnection(host: NWEndpoint.Host(host), port: NWEndpoint.Port(rawValue: port)!, using: parameters)
    }

    func connect(timeout: Double = 10) async throws {
        let endpoint = endpoint, pinCheck = pinCheck, label = label
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            // state handlers for this connection run serially on this dedicated queue.
            let queue = DispatchQueue(label: "ShareCompute.connect.\(UUID().uuidString)")
            var finished = false
            // A refused, unroutable or Local Network-denied connection waits and retries rather than
            // failing, so the waiting reason is the only record of why it never became ready.
            var waiting: String?
            let began = Date()
            connection.stateUpdateHandler = { state in
                guard !finished else { return }
                switch state {
                case .ready:
                    finished = true; continuation.resume()
                    if let label { EventLog.shared.event("net_ready", ["stream": .text(label), "ms": .ms(Date().timeIntervalSince(began))]) }
                case .waiting(let error):
                    // Logged as it happens: this is the only place a denied Local Network
                    // permission or a refused connection is visible before the timeout.
                    let reason = describe(error)
                    if let label, reason != waiting {
                        EventLog.shared.event("net_waiting", ["stream": .text(label), "endpoint": .text(endpoint), "reason": .text(reason)])
                    }
                    waiting = reason
                case .failed(let error):
                    finished = true
                    if let label {
                        EventLog.shared.event("net_failed", ["stream": .text(label), "endpoint": .text(endpoint),
                                                             "reason": .text(describe(error)), "pin_mismatch": .bool(pinCheck.rejected)])
                    }
                    // The laptop answered; only its certificate was wrong, so network advice would mislead.
                    continuation.resume(throwing: pinCheck.rejected ? WorkerError.wrongCertificate
                        : WorkerError.unreachable(endpoint: endpoint, reason: describe(error)))
                case .cancelled:
                    finished = true
                    if let label {
                        EventLog.shared.event("net_cancelled", ["stream": .text(label), "endpoint": .text(endpoint),
                                                                "last_waiting_reason": .text(waiting), "pin_mismatch": .bool(pinCheck.rejected),
                                                                "after_ms": .ms(Date().timeIntervalSince(began))])
                    }
                    continuation.resume(throwing: pinCheck.rejected ? WorkerError.wrongCertificate
                        : WorkerError.unreachable(endpoint: endpoint, reason: waiting ?? "no answer within \(Int(timeout)) seconds"))
                default: break
                }
            }
            connection.start(queue: queue)
            queue.asyncAfter(deadline: .now() + timeout) { [weak self] in
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
    // A change this large in one step is a paste, scan or file import, not typing; typing would
    // otherwise write a line per keystroke.
    @Published var pairingText = "" {
        didSet {
            let jump = abs(pairingText.count - oldValue.count)
            guard jump >= 16 else { return }
            // Only the length and the kind of text are recorded, never the text.
            let kind = pairingText.hasPrefix("sc1.") ? "sc1 code" : (pairingText.trimmingCharacters(in: .whitespacesAndNewlines).hasPrefix("{") ? "json" : "other")
            EventLog.shared.event("pairing_text_set", ["chars": .num(pairingText.count), "kind": .text(kind)])
        }
    }
    @Published var status = "Start on your laptop, then scan its iPhone QR."
    @Published var running = false
    @Published var allocated: UInt64 = 0
    @Published var peak: UInt64 = 0
    @Published var graphs: UInt64 = 0
    @Published var cachedBytes: UInt64 = 0
    @Published var cacheHits: UInt64 = 0
    /// Weights the laptop sent before, so a repeat run with the same model skips most of the upload.
    /// The native worker checks each file against its hash before use; iOS may purge Caches when
    /// storage runs low, which only costs a re-upload.
    static let cacheDirectory = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("rpc-weights", isDirectory: true)
    private var runner: Task<Void, Never>?
    private var heartbeat: Task<Void, Never>?
    private var channels: [String: Task<Void, Never>] = [:]
    private var streams: [UUID: Stream] = [:]
    private var nativePort: UInt16?
    private var nativeBudget: UInt64?
    private var epoch = UUID()
    private var sessionStarted: Date?
    private var sessionID = ""
    private var pulse = Pulse()

    /// What the heartbeat loop remembers between iterations so telemetry can be sampled rather than
    /// written every 500 ms. Main-actor state: the loop that updates it runs on the main actor.
    private struct Pulse {
        var lastTelemetry = Date.distantPast
        var lastCacheLine = Date.distantPast
        var loggedAllocated: UInt64 = 0
        var cacheHit: UInt64 = 0, cacheStored: UInt64 = 0, cacheRejected: UInt64 = 0
        var maxSend: TimeInterval = 0, maxLate: TimeInterval = 0
    }
    // Telemetry sampling (the reasoning is in `notePulse`).
    private static let telemetryInterval: TimeInterval = 10
    private static let cacheInterval: TimeInterval = 5
    private static let allocationStep: UInt64 = 128 * 1_048_576
    private static let stallThreshold: TimeInterval = 2

    init() {
        // The first thing the app does, so a launch is on record even if nothing else happens.
        EventLog.shared.event("app_launch", Diagnostics.launchFields(), flush: true)
        if let prior = EventLog.shared.prior { EventLog.shared.event("prior_launch", prior) }
        // The native commit comes from the linked library and the generated constant from the Swift
        // sources; they are written by the same build, so a difference means a stale artifact.
        let native = BuildInfo.native, generated = BuildInfo.generated
        if native != BuildInfo.unknown, generated != BuildInfo.unknown, native != generated {
            EventLog.shared.event("build_mismatch", ["native_build": .text(native), "generated_runtime_build": .text(generated)])
        }
        LifecycleLog.install()
    }

    func startSimulatorIfRequested() {
        #if targetEnvironment(simulator)
        if !running, let encoded = ProcessInfo.processInfo.environment["SC_PAIRING_B64"],
           let data = Data(base64Encoded: encoded), let text = String(data: data, encoding: .utf8) {
            EventLog.shared.event("pairing_source", ["source": "simulator environment"])
            pairingText = text; start()
        }
        #endif
    }

    /// Why a pairing was refused, for the log only. The operator still sees `invalidPairing`; this
    /// names the failed check without quoting any value that could be a secret. It is evaluated
    /// only after the real validation in `start()` has already failed.
    private static func pairingProblem(_ pair: Pairing) -> String {
        if pair.node != "iphone" { return "node is \"\(pair.node)\", not iphone" }
        if pair.port == 0 { return "port is 0" }
        if pair.runtime != runtimeRevision { return "pairing runtime \(pair.runtime) differs from this app's \(runtimeRevision)" }
        let native = String(cString: sc_worker_revision())
        if pair.runtime != native { return "pairing runtime \(pair.runtime) differs from the native library's \(native)" }
        if pair.pin.count != 64 { return "pin is \(pair.pin.count) characters, expected 64" }
        if !pair.pin.allSatisfy({ $0.isHexDigit && !$0.isUppercase }) { return "pin is not lowercase hex" }
        if pair.token.count != 64 { return "token is \(pair.token.count) characters, expected 64" }
        if !(64...4096).contains(pair.budget_mib) { return "budget \(pair.budget_mib) MiB is outside 64 to 4096" }
        return "no specific check identified"
    }

    private static func decodeProblem(_ error: Error) -> String {
        guard let decoding = error as? DecodingError else { return "pairing text could not be read" }
        func path(_ context: DecodingError.Context) -> String { context.codingPath.map { $0.stringValue }.joined(separator: ".") }
        switch decoding {
        case .keyNotFound(let key, _): return "field \"\(key.stringValue)\" is missing"
        case .valueNotFound(_, let context): return "field \"\(path(context))\" has no value"
        case .typeMismatch(_, let context): return "field \"\(path(context))\" has the wrong type"
        case .dataCorrupted: return "text is not valid JSON"
        @unknown default: return "pairing text could not be decoded"
        }
    }

    func start() {
        guard !running else { return }
        EventLog.shared.event("start_requested", ["pairing_chars": .num(pairingText.count)])
        var rejection = "pairing text could not be read"
        do {
            var text = pairingText.trimmingCharacters(in: .whitespacesAndNewlines)
            guard text.count <= 8192 else { rejection = "pairing text is \(text.count) characters; the limit is 8192"; throw WorkerError.invalidPairing }
            if text.hasPrefix("sc1.") {
                var encoded = String(text.dropFirst(4)).replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
                encoded += String(repeating: "=", count: (4 - encoded.count % 4) % 4)
                guard let decoded = Data(base64Encoded: encoded), let json = String(data: decoded, encoding: .utf8) else {
                    rejection = "sc1 code is not valid base64 text"; throw WorkerError.invalidPairing
                }
                text = json
            }
            let pair: Pairing
            do { pair = try JSONDecoder().decode(Pairing.self, from: Data(text.utf8)) }
            catch { rejection = Self.decodeProblem(error); throw error }
            guard pair.node == "iphone", pair.port > 0, pair.runtime == runtimeRevision,
                  pair.runtime == String(cString: sc_worker_revision()), pair.pin.count == 64,
                  pair.pin.allSatisfy({ $0.isHexDigit && !$0.isUppercase }), pair.token.count == 64,
                  (64...4096).contains(pair.budget_mib) else { rejection = Self.pairingProblem(pair); throw WorkerError.invalidPairing }
            if let nativeBudget, nativeBudget != pair.budget_mib {
                EventLog.shared.event("pairing_rejected", ["reason": .text("the native listener keeps its \(nativeBudget) MiB budget until the app restarts; this pairing asks for \(pair.budget_mib) MiB")])
                status = "Restart the app to change its native memory budget."; return
            }
            EventLog.shared.event("pairing_ok", ["node": .text(pair.node), "endpoint": .text("\(pair.host):\(pair.port)"),
                                                 "runtime": .text(pair.runtime), "budget_mib": .num(pair.budget_mib),
                                                 "pin": "redacted", "token": "redacted"])
            if nativePort == nil {
                let port = UInt16.random(in: 20000...30000); nativePort = port; nativeBudget = pair.budget_mib
                let cachePath = Self.cacheDirectory.path
                let threads: Int32 = 2
                EventLog.shared.event("native_start", ["port": .num(port), "budget_mib": .num(pair.budget_mib), "threads": .num(threads),
                                                       "weight_cache": "on", "cache_dir": .text(Self.cacheDirectory.lastPathComponent)],
                                      flush: true)
                Thread.detachNewThread {
                    // sc_worker_run never returns while healthy, so the C string outlives the server.
                    let result = cachePath.withCString { sc_worker_run(Int32(port), pair.budget_mib * 1_048_576, threads, $0) }
                    EventLog.shared.event("native_exit", ["result": .num(result)], flush: true)
                    Task { @MainActor [weak self] in self?.stop("Native worker exited (\(result)); restart the app", cause: "native_exit") }
                }
            } else {
                EventLog.shared.event("native_reuse", ["port": .num(nativePort ?? 0), "budget_mib": .num(pair.budget_mib)])
            }
            running = true; epoch = UUID(); let current = epoch
            sessionStarted = Date(); sessionID = UUID().uuidString; pulse = Pulse()
            pulse.cacheHit = sc_worker_cache_hit_bytes(); pulse.cacheStored = sc_worker_cache_stored_bytes()
            pulse.cacheRejected = sc_worker_cache_rejected(); pulse.lastTelemetry = Date()
            setIdleTimer(disabled: true, reason: "session started")
            status = "Connecting…"
            runner = Task { [weak self] in
                guard let self else { return }
                do { try await self.run(pair, current: current) }
                catch { if self.epoch == current { self.stop("Disconnected: \(error.localizedDescription)", cause: "control_failed", error: error) } }
            }
        } catch {
            let reason: LogFields = ["reason": .text(rejection)]
            EventLog.shared.event("pairing_rejected", reason + errorFields(error))
            status = "Invalid pairing file: \(error.localizedDescription)"
        }
    }

    /// The idle timer keeps the screen from locking during a session, and a locked screen is one of
    /// the ways a phone goes silent, so each change is on record with the state actually in effect.
    private func setIdleTimer(disabled: Bool, reason: String) {
        UIApplication.shared.isIdleTimerDisabled = disabled
        EventLog.shared.event("idle_timer", ["disabled": .bool(UIApplication.shared.isIdleTimerDisabled), "reason": .text(reason)])
    }

    func refreshCacheSize() {
        let files = (try? FileManager.default.contentsOfDirectory(at: Self.cacheDirectory, includingPropertiesForKeys: [.fileSizeKey])) ?? []
        cachedBytes = files.reduce(0) { $0 + UInt64((try? $1.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0) }
    }

    /// Safe while connected: a file deleted mid-run reads as a miss and the laptop sends it again.
    func clearCache() {
        let before = cachedBytes
        let files = (try? FileManager.default.contentsOfDirectory(at: Self.cacheDirectory, includingPropertiesForKeys: nil)) ?? []
        for file in files { try? FileManager.default.removeItem(at: file) }
        refreshCacheSize()
        EventLog.shared.event("cache_cleared", ["cleared_mib": .num((before - min(before, cachedBytes)) / 1_048_576), "running": .bool(running)])
        status = "Cleared \((before - min(before, cachedBytes)) / 1_048_576) MiB. The next test uploads the model again."
    }

    /// Ends the session. `cause` is a stable machine-readable label; `message` is the operator's
    /// sentence. Both are logged, with the error that triggered it, so a disconnect is never
    /// recorded without its reason.
    func stop(_ message: String, cause: String = "ui_request", error: Error? = nil, detail: LogFields = [:]) {
        print("SC_IOS \(message)")
        let wasRunning = running
        var fields: LogFields = ["message": .text(message), "cause": .text(cause), "was_running": .bool(wasRunning),
                                 "open_channels": .num(channels.count)]
        fields = fields + detail
        if let error { fields = fields + errorFields(error) }
        if wasRunning, let sessionStarted {
            let elapsed: LogFields = ["session_ms": .ms(Date().timeIntervalSince(sessionStarted))]
            fields = fields + elapsed + counterFields()
        }
        EventLog.shared.event("disconnect", fields + Diagnostics.pressure(), flush: true)
        if wasRunning { noteCache(force: true) }
        epoch = UUID(); running = false; status = message
        runner?.cancel(); heartbeat?.cancel()
        for task in channels.values { task.cancel() }
        for stream in streams.values { stream.close() }
        streams.removeAll(); channels.removeAll()
        if wasRunning || UIApplication.shared.isIdleTimerDisabled { setIdleTimer(disabled: false, reason: "session ended: \(cause)") }
        refreshCacheSize()
    }
    private func registered(_ stream: Stream) -> UUID { let id = UUID(); streams[id] = stream; return id }

    // MARK: Telemetry

    private func counterFields() -> LogFields {
        ["allocated_bytes": .num(sc_worker_allocated()), "peak_bytes": .num(sc_worker_peak()), "graph_calls": .num(sc_worker_graphs()),
         "cache_hit_bytes": .num(sc_worker_cache_hit_bytes()), "cache_stored_bytes": .num(sc_worker_cache_stored_bytes()),
         "cache_rejected": .num(sc_worker_cache_rejected())]
    }

    /// Weight-cache decisions. The native worker exposes cumulative counters, not one record per
    /// file, so a decision is seen as a change in a counter: bytes stored, bytes served from cache,
    /// files rejected. Changes are coalesced into one line per `cacheInterval` while they keep
    /// arriving (a cold upload changes the counters on every file), with the delta since the last
    /// line. A rejection is rare and means a stored file failed its hash, so it is never delayed.
    private func noteCache(force: Bool = false) {
        let hit = sc_worker_cache_hit_bytes(), stored = sc_worker_cache_stored_bytes(), rejected = sc_worker_cache_rejected()
        let changed = hit != pulse.cacheHit || stored != pulse.cacheStored || rejected != pulse.cacheRejected
        guard changed else { return }
        let now = Date()
        let rejection = rejected > pulse.cacheRejected
        guard force || rejection || now.timeIntervalSince(pulse.lastCacheLine) >= Self.cacheInterval else { return }
        EventLog.shared.event("cache", ["hit_bytes_delta": .num(hit &- pulse.cacheHit), "stored_bytes_delta": .num(stored &- pulse.cacheStored),
                                        "rejected_delta": .num(rejected &- pulse.cacheRejected), "hit_bytes": .num(hit),
                                        "stored_bytes": .num(stored), "rejected": .num(rejected), "rejection": .bool(rejection)])
        pulse.cacheHit = hit; pulse.cacheStored = stored; pulse.cacheRejected = rejected; pulse.lastCacheLine = now
    }

    /// Called after each heartbeat. Writes a line only when something is worth one:
    ///  - `telemetry` every 10 s. A run is minutes long, so this is dozens of lines rather than a
    ///    line per 500 ms heartbeat (about 1,000 for an 8-minute run), while still giving a memory
    ///    and temperature time series fine enough to line up with a failure.
    ///  - `telemetry` early when allocation moved 128 MiB since the last line. Model load is when
    ///    memory rises by a gigabyte in seconds, and a 10 s grid would miss its shape. The count is
    ///    bounded by the worker's own budget (at most 4,096 MiB), so it cannot flood.
    ///  - `heartbeat_late` / `heartbeat_send_slow` whenever one is over 2 s. These separate the two
    ///    causes F42 could not tell apart: the sleep overran (this app was frozen or the main thread
    ///    was busy) versus the send was slow (the network or the laptop). They are rare by
    ///    construction, because a healthy loop is 0.5 s.
    ///  - `cache` lines, coalesced (see `noteCache`).
    ///
    /// Runs on the main actor because the heartbeat task does, which is also what makes reading
    /// `UIApplication.shared` here legal.
    private func notePulse(send: TimeInterval, overrun: TimeInterval) {
        pulse.maxSend = max(pulse.maxSend, send); pulse.maxLate = max(pulse.maxLate, overrun)
        let state: String
        switch UIApplication.shared.applicationState {
        case .active: state = "active"
        case .inactive: state = "inactive"
        case .background: state = "background"
        @unknown default: state = "unknown"
        }
        if overrun > Self.stallThreshold {
            let late: LogFields = ["late_ms": .ms(overrun), "expected_ms": 500, "app_state": .text(state)]
            EventLog.shared.event("heartbeat_late", late + Diagnostics.pressure())
        }
        if send > Self.stallThreshold {
            EventLog.shared.event("heartbeat_send_slow", ["send_ms": .ms(send), "app_state": .text(state)])
        }
        noteCache()
        let now = Date()
        let allocatedNow = sc_worker_allocated()
        let moved = allocatedNow > pulse.loggedAllocated ? allocatedNow - pulse.loggedAllocated : pulse.loggedAllocated - allocatedNow
        guard now.timeIntervalSince(pulse.lastTelemetry) >= Self.telemetryInterval || moved >= Self.allocationStep else { return }
        let fields: LogFields = ["app_state": .text(state), "idle_timer_disabled": .bool(UIApplication.shared.isIdleTimerDisabled),
                                 "channels": .num(channels.count), "max_send_ms": .ms(pulse.maxSend), "max_late_ms": .ms(pulse.maxLate)]
        EventLog.shared.event("telemetry", fields + counterFields() + Diagnostics.pressure())
        pulse.lastTelemetry = now; pulse.loggedAllocated = allocatedNow; pulse.maxSend = 0; pulse.maxLate = 0
    }

    private func run(_ pair: Pairing, current: UUID) async throws {
        guard let port = nativePort else { throw WorkerError.protocolError }
        // Listener creation happens on the native thread; retry local readiness briefly.
        var ready = false
        var attempts = 0
        let probeStarted = Date()
        for _ in 0..<40 {
            try Task.checkCancellation()
            attempts += 1
            let probe = Stream(host: "127.0.0.1", port: port)
            do { try await probe.connect(timeout: 1); ready = true } catch { }
            probe.close()
            if ready { break }; try await Task.sleep(nanoseconds: 50_000_000)
        }
        EventLog.shared.event(ready ? "native_ready" : "native_not_ready", ["attempts": .num(attempts), "ms": .ms(Date().timeIntervalSince(probeStarted))])
        guard ready else { throw WorkerError.nativeNotListening }
        let control = Stream(host: pair.host, port: pair.port, pin: pair.pin, label: "control"); let id = registered(control)
        defer { control.close(); streams.removeValue(forKey: id) }
        // The first LAN connection raises iOS's Local Network prompt; leave time to answer it.
        status = "Connecting to laptop at \(pair.host):\(pair.port)… If iOS asks to find devices on your local network, tap Allow."
        let connectStarted = Date()
        EventLog.shared.event("connect_start", ["endpoint": .text("\(pair.host):\(pair.port)"), "timeout_s": 45])
        try await control.connect(timeout: 45); try Task.checkCancellation()
        EventLog.shared.event("connect_ready", ["ms": .ms(Date().timeIntervalSince(connectStarted))])
        #if targetEnvironment(simulator)
        let simulator = true
        #else
        let simulator = false
        #endif
        // `build` is the native runtime's commit and `app_build` is this app's own; the laptop
        // prints both and refuses a PASS when known commits differ. "unknown" means no provenance.
        let nativeBuild = BuildInfo.native, appBuild = BuildInfo.app
        try await control.sendJSON(["kind": "control", "node": pair.node, "token": pair.token,
                                    "runtime": pair.runtime, "platform": "ios", "simulator": simulator,
                                    "budget_mib": pair.budget_mib, "session": sessionID,
                                    "build": nativeBuild, "app_build": appBuild])
        let joinSent = Date()
        EventLog.shared.event("join_sent", ["session": .text(sessionID), "platform": "ios", "simulator": .bool(simulator),
                                            "budget_mib": .num(pair.budget_mib), "runtime": .text(pair.runtime),
                                            "build": .text(nativeBuild), "app_build": .text(appBuild)])
        let ack: [String: Any]
        // The laptop closes an unauthenticated, duplicate or mismatched join without replying.
        do { ack = try await control.receiveJSON() }
        catch WorkerError.connectionClosed {
            EventLog.shared.event("join_rejected", ["detail": "the laptop closed the connection without replying",
                                                    "after_ms": .ms(Date().timeIntervalSince(joinSent))])
            throw WorkerError.rejectedByLaptop
        }
        guard ack["ok"] as? Bool == true else {
            EventLog.shared.event("join_rejected", ["detail": "the laptop replied with ok not true", "after_ms": .ms(Date().timeIntervalSince(joinSent))])
            throw WorkerError.rejectedByLaptop
        }
        let accepted: LogFields = ["ms": .ms(Date().timeIntervalSince(joinSent))]
        EventLog.shared.event("join_accepted", accepted + counterFields() + Diagnostics.pressure())
        status = "Connected — waiting for model layers"
        print("SC_IOS connected; native worker ready")
        heartbeat = Task { [weak self] in
            guard let self else { return }
            do {
                while !Task.isCancelled {
                    self.allocated = sc_worker_allocated(); self.peak = sc_worker_peak(); self.graphs = sc_worker_graphs()
                    self.cacheHits = sc_worker_cache_hit_bytes()
                    let sendStarted = Date()
                    try await control.sendJSON(["op": "stats", "allocated_bytes": self.allocated,
                                                "peak_bytes": self.peak, "graph_calls": self.graphs,
                                                "cache_hit_bytes": self.cacheHits,
                                                "cache_stored_bytes": sc_worker_cache_stored_bytes(),
                                                "cache_rejected": sc_worker_cache_rejected()])
                    let sleepStarted = Date()
                    try await Task.sleep(nanoseconds: 500_000_000)
                    self.notePulse(send: sleepStarted.timeIntervalSince(sendStarted),
                                   overrun: Date().timeIntervalSince(sleepStarted) - 0.5)
                }
            } catch { if self.epoch == current && !Task.isCancelled { self.stop("Heartbeat failed", cause: "heartbeat_send_failed", error: error) } }
        }
        while !Task.isCancelled {
            let message = try await control.receiveJSON()
            guard message["op"] as? String == "open", let channel = message["channel"] as? String,
                  channel.count == 32, channels.count < 4, channels[channel] == nil else {
                EventLog.shared.event("protocol_error", ["detail": "unexpected control message",
                                                         "op": .text(message["op"] as? String), "open_channels": .num(channels.count)])
                throw WorkerError.protocolError
            }
            // Only the first 8 characters of a channel id are recorded; the full id is part of the
            // data-channel handshake.
            let tag = String(channel.prefix(8))
            EventLog.shared.event("channel_requested", ["channel": .text(tag), "open_channels": .num(channels.count)])
            channels[channel] = Task { [weak self] in
                guard let self else { return }
                defer { self.channels.removeValue(forKey: channel) }
                do { try await self.tunnel(pair, port: port, channel: channel) }
                catch {
                    if self.epoch == current && !Task.isCancelled {
                        self.stop("Compute tunnel failed: \(error.localizedDescription)", cause: "tunnel_failed", error: error,
                                  detail: ["channel": .text(tag)])
                    }
                }
            }
        }
    }
    private func tunnel(_ pair: Pairing, port: UInt16, channel: String) async throws {
        let tag = String(channel.prefix(8))
        let remote = Stream(host: pair.host, port: pair.port, pin: pair.pin, label: "channel-laptop")
        let local = Stream(host: "127.0.0.1", port: port, label: "channel-native")
        let remoteID = registered(remote); let localID = registered(local)
        // Bytes read from the laptop and written to the native worker, and the reverse.
        let fromLaptop = ByteCount(), toLaptop = ByteCount(), ended = FirstReason()
        let opened = Date()
        defer {
            remote.close(); local.close(); streams.removeValue(forKey: remoteID); streams.removeValue(forKey: localID)
            EventLog.shared.event("channel_closed", ["channel": .text(tag), "ms": .ms(Date().timeIntervalSince(opened)),
                                                     "bytes_from_laptop": .num(fromLaptop.value), "bytes_to_laptop": .num(toLaptop.value),
                                                     "ended_by": .text(ended.value ?? "error or cancellation")])
        }
        try await remote.connect()
        try await remote.sendJSON(["kind": "data", "node": pair.node, "token": pair.token, "channel": channel])
        let ack = try await remote.receiveJSON()
        guard ack["ok"] as? Bool == true else {
            EventLog.shared.event("protocol_error", ["detail": "the laptop refused the data channel", "channel": .text(tag)])
            throw WorkerError.protocolError
        }
        try await local.connect()
        EventLog.shared.event("channel_open", ["channel": .text(tag), "setup_ms": .ms(Date().timeIntervalSince(opened))])
        try await withThrowingTaskGroup(of: Void.self) { group in
            group.addTask {
                while let data = try await remote.read() { try await local.write(data); fromLaptop.add(data.count) }
                ended.set("the laptop closed its side")
            }
            group.addTask {
                while let data = try await local.read() { try await remote.write(data); toLaptop.add(data.count) }
                ended.set("the native worker closed its side")
            }
            do { try await group.next() }
            catch { remote.close(); local.close(); group.cancelAll(); throw error }
            remote.close(); local.close(); group.cancelAll()
            // Cancellation closes the opposite receive; its cancellation error is expected after EOF.
            while !group.isEmpty { _ = try? await group.next() }
        }
    }
}
