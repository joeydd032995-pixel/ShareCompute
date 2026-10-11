import Foundation
import os
import UIKit

// The iPhone's event log (F42 addendum, F43).
//
// An operator once reported `iPhone control disconnected: TimeoutError`, and the cause can never be
// settled because no device kept a log of the failure, least of all the iPhone, which is the device
// that appeared to go silent. This file is what the next failure leaves behind.
//
// The rules, in order of importance:
//   1. Logging never fails a run. Nothing here throws to a caller, and every file operation is a
//      dropped line on failure, never an error.
//   2. A kill still leaves history. Each line is written to a file as it happens, in the app's
//      Documents folder (the app already sets UIFileSharingEnabled, so the Files app can reach it),
//      and the file is appended to across launches rather than truncated.
//   3. No secrets. Callers never pass the token, pin or pairing text, and `EventLog.scrub` removes
//      anything shaped like one as a second line of defence.

/// A value a log line can carry. Kept to JSON's own types so a line can be written without
/// `JSONSerialization`, which raises on NaN and cannot keep key order.
enum LogValue: Sendable, ExpressibleByStringLiteral, ExpressibleByIntegerLiteral,
               ExpressibleByBooleanLiteral, ExpressibleByFloatLiteral {
    case string(String), int(Int64), double(Double), bool(Bool), null
    init(stringLiteral value: String) { self = .string(value) }
    init(integerLiteral value: Int64) { self = .int(value) }
    init(booleanLiteral value: Bool) { self = .bool(value) }
    init(floatLiteral value: Double) { self = .double(value) }
    static func text(_ value: String?) -> LogValue { value.map { LogValue.string($0) } ?? .null }
    /// Any integer. A value that does not fit in Int64 becomes null rather than trapping.
    static func num<T: BinaryInteger>(_ value: T) -> LogValue { Int64(exactly: value).map { LogValue.int($0) } ?? .null }
    /// Seconds, written as milliseconds to one decimal place, like the coordinator's `at_ms`.
    static func ms(_ seconds: Double) -> LogValue { .double((seconds * 10_000).rounded() / 10) }
    static func mib(_ bytes: Int?) -> LogValue { bytes.map { LogValue.int(Int64($0 / 1_048_576)) } ?? .null }
}

/// Ordered key/value pairs, so `["port": .num(p), "budget_mib": .num(b)]` keeps its order on disk.
struct LogFields: ExpressibleByDictionaryLiteral, Sendable {
    var pairs: [(String, LogValue)]
    init(dictionaryLiteral elements: (String, LogValue)...) { pairs = elements }
    static func + (lhs: LogFields, rhs: LogFields) -> LogFields {
        var merged = lhs; merged.pairs.append(contentsOf: rhs.pairs); return merged
    }
}

/// One JSON object per line, with the same `at_ms` and `ev` keys as the coordinator's
/// `scripts/run_log.py` `EventLog`, so the two files can be read side by side.
///
///     {"at_ms":12034.5,"ts":"2026-10-10T14:03:11.250Z","launch":"3f9a1c2e","n":41,"ev":"join_ack","ms":96.2}
///
/// - `at_ms`: milliseconds since this launch opened the log, measured on the wall clock. It keeps
///   counting while the app is suspended, which is the point: a gap in `at_ms` is time the phone
///   was not running. The coordinator's `at_ms` is relative to its own start, so compare durations
///   across the two files, not values.
/// - `ts`: absolute UTC time. The phone's and laptop's clocks are not synchronised, so expect an
///   offset of up to a few seconds when lining the files up.
/// - `launch`: random per process start; one file holds several launches.
/// - `n`: 1, 2, 3... within a launch. A gap in `n` means lines were lost.
///
/// Concurrency: `@unchecked Sendable`, and that is a claim about this class, not a shortcut. Every
/// mutable property is read and written only on `queue`, a private serial queue. `event` is
/// callable from any thread or actor because it only reads its arguments, builds a string, and
/// hands the string to the queue. The one synchronous path (`flush: true`, export) uses
/// `queue.sync` and is never called from the queue itself, so it cannot deadlock.
final class EventLog: @unchecked Sendable {
    static let shared = EventLog()

    // Ring: about 3,000 lines (a 10-minute run is a few hundred) and at most 2 MB, so the memory
    // cost is bounded whatever the lines contain. Each string value is also cut at 512 characters.
    static let maxRingLines = 3000
    static let maxRingBytes = 2_000_000
    // File: rotated to events.previous.jsonl past this size, so the disk cost is at most about 4 MB.
    static let maxFileBytes = 2_000_000
    static let maxValueCharacters = 512

    let launchID = String(UUID().uuidString.prefix(8)).lowercased()
    /// What the previous launch's file ended with, for `prior_launch`. Read before this launch wrote anything.
    let prior: LogFields?

    private let started = Date()
    private let queue = DispatchQueue(label: "ShareCompute.eventlog", qos: .utility)
    private let directory: URL?
    // Touched only on `queue` (and in init, before the object is shared).
    private var ring: [String] = []
    private var ringBytes = 0
    private var sequence = 0
    private var descriptor: Int32 = -1
    private var fileBytes = 0
    private var consecutiveFailures = 0
    private var gaveUp = false
    private var fileDegraded = false

    private init() {
        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first
        let folder = documents?.appendingPathComponent("logs", isDirectory: true)
        var summary: LogFields?
        if var folder {
            try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            // A log is not worth an iCloud backup slot.
            var values = URLResourceValues(); values.isExcludedFromBackup = true
            try? folder.setResourceValues(values)
            summary = Self.summarisePriorLaunch(at: folder.appendingPathComponent("events.jsonl").path)
        }
        directory = folder
        prior = summary
        openFile()
        if fileBytes > Self.maxFileBytes { rotate() }
        if directory == nil || descriptor < 0 { fileDegraded = true }
    }

    // MARK: Writing

    /// Record one event. Safe from any thread, actor or queue; never throws; never blocks on disk
    /// unless `flush` is set.
    ///
    /// Pass `flush: true` only for moments the OS may freeze the process straight afterwards
    /// (resigning active, entering the background, a memory warning): it waits for the lines already
    /// queued, so they are in the file before suspension.
    func event(_ name: String, _ fields: LogFields = [:], flush: Bool = false) {
        let now = Date()
        let atMs = (now.timeIntervalSince(started) * 10_000).rounded() / 10
        let stamp = Self.stamp(now)
        var body = ""
        for (key, value) in fields.pairs { body += ",\(Self.quote(key)):\(Self.render(value))" }
        let finished = body
        if flush {
            queue.sync { self.record(atMs: atMs, stamp: stamp, name: name, body: finished) }
        } else {
            queue.async { self.record(atMs: atMs, stamp: stamp, name: name, body: finished) }
        }
    }

    /// The finished export: every launch still on disk, oldest first. Writes a temporary file and
    /// returns its URL. Called when the operator taps Share, never from the session path.
    func makeExportFile() throws -> URL {
        let text: String = queue.sync {
            let useFile = !fileDegraded
            record(atMs: (Date().timeIntervalSince(started) * 10_000).rounded() / 10, stamp: Self.stamp(Date()),
                   name: "export", body: ",\"source\":\(Self.quote(useFile ? "file" : "memory")),\"ring_lines\":\(ring.count)")
            if useFile, let directory {
                var combined = Data()
                for name in ["events.previous.jsonl", "events.jsonl"] {
                    if let part = try? Data(contentsOf: directory.appendingPathComponent(name)) { combined.append(part) }
                }
                if !combined.isEmpty { return String(decoding: combined, as: UTF8.self) }
            }
            // The file could not be kept; the ring still holds this launch.
            return ring.joined(separator: "\n") + "\n"
        }
        let folder = FileManager.default.temporaryDirectory
        // Older exports are only clutter once a newer one exists.
        for old in (try? FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: nil)) ?? []
        where old.lastPathComponent.hasPrefix("sharecompute-iphone-events-") {
            try? FileManager.default.removeItem(at: old)
        }
        let url = folder.appendingPathComponent("sharecompute-iphone-events-\(Self.fileStamp(Date())).txt")
        try Data(text.utf8).write(to: url, options: .atomic)
        return url
    }

    // MARK: Queue-confined internals

    private func record(atMs: Double, stamp: String, name: String, body: String) {
        sequence += 1
        let line = "{\"at_ms\":\(atMs),\"ts\":\"\(stamp)\",\"launch\":\"\(launchID)\",\"n\":\(sequence),\"ev\":\(Self.quote(name))\(body)}"
        ring.append(line); ringBytes += line.utf8.count
        while !ring.isEmpty && (ring.count > Self.maxRingLines || ringBytes > Self.maxRingBytes) {
            ringBytes -= ring.removeFirst().utf8.count
        }
        append(line)
        // The simulator job captures the app's console, so this is how CI sees the log.
        print("SC_LOG " + line)
    }

    private var currentURL: URL? { directory?.appendingPathComponent("events.jsonl") }
    private var previousURL: URL? { directory?.appendingPathComponent("events.previous.jsonl") }

    private func noteFailure() {
        fileDegraded = true; consecutiveFailures += 1
        if descriptor >= 0 { Darwin.close(descriptor); descriptor = -1 }
        if consecutiveFailures >= 20 { gaveUp = true }
    }

    private func openFile() {
        guard descriptor < 0, let url = currentURL else { return }
        let opened = Darwin.open(url.path, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC, 0o644)
        if opened < 0 { noteFailure(); return }
        descriptor = opened
        let size = lseek(opened, 0, SEEK_END)
        fileBytes = size > 0 ? Int(size) : 0
    }

    private func rotate() {
        if descriptor >= 0 { Darwin.close(descriptor); descriptor = -1 }
        if let current = currentURL, let previous = previousURL {
            try? FileManager.default.removeItem(at: previous)
            try? FileManager.default.moveItem(at: current, to: previous)
        }
        fileBytes = 0; openFile()
    }

    private func append(_ line: String) {
        guard directory != nil, !gaveUp else { return }
        if descriptor < 0 { openFile() }
        if fileBytes > Self.maxFileBytes { rotate() }
        guard descriptor >= 0 else { return }
        var bytes = Array(line.utf8); bytes.append(10)
        var offset = 0
        while offset < bytes.count {
            let written = bytes[offset...].withUnsafeBytes { Darwin.write(descriptor, $0.baseAddress, $0.count) }
            if written < 0 {
                if errno == EINTR { continue }
                noteFailure(); return
            }
            offset += written
        }
        fileBytes += bytes.count; consecutiveFailures = 0
    }

    // MARK: Formatting

    private static func render(_ value: LogValue) -> String {
        switch value {
        case .string(let text): return quote(text)
        case .int(let number): return String(number)
        case .double(let number): return number.isFinite ? String(number) : "null"
        case .bool(let flag): return flag ? "true" : "false"
        case .null: return "null"
        }
    }

    private static func quote(_ raw: String) -> String {
        var text = scrub(raw)
        if text.count > maxValueCharacters { text = String(text.prefix(maxValueCharacters)) + "…" }
        var out = "\""
        for scalar in text.unicodeScalars {
            switch scalar {
            case "\"": out += "\\\""
            case "\\": out += "\\\\"
            case "\n": out += "\\n"
            case "\r": out += "\\r"
            case "\t": out += "\\t"
            default:
                if scalar.value < 0x20 { out += String(format: "\\u%04x", scalar.value) } else { out.unicodeScalars.append(scalar) }
            }
        }
        return out + "\""
    }

    /// Second line of defence against a secret reaching the log. The pairing token and the
    /// certificate pin are both exactly 64 hex characters, and a pairing code starts `sc1.`; call
    /// sites do not pass any of them, and this replaces anything shaped like one if one slips in.
    /// A 40-hex commit is deliberately left alone: those are wanted.
    static func scrub(_ text: String) -> String {
        let bytes = Array(text.utf8)
        func isHex(_ b: UInt8) -> Bool {
            switch b { case 48...57, 65...70, 97...102: return true; default: return false }
        }
        // base64url alphabet plus padding: what follows `sc1.` in a pairing code.
        func isCode(_ b: UInt8) -> Bool {
            switch b { case 48...57, 65...90, 97...122, 45, 95, 61: return true; default: return false }
        }
        var out = [UInt8](); out.reserveCapacity(bytes.count)
        var i = 0
        while i < bytes.count {
            if bytes[i] == 115, i + 4 <= bytes.count, bytes[i + 1] == 99, bytes[i + 2] == 49, bytes[i + 3] == 46 {
                var j = i + 4
                while j < bytes.count && isCode(bytes[j]) { j += 1 }
                if j - (i + 4) >= 16 { out.append(contentsOf: Array("[redacted pairing code]".utf8)); i = j; continue }
            }
            if isHex(bytes[i]) {
                var j = i
                while j < bytes.count && isHex(bytes[j]) { j += 1 }
                if j - i >= 64 { out.append(contentsOf: Array("[redacted]".utf8)) } else { out.append(contentsOf: bytes[i..<j]) }
                i = j
            } else { out.append(bytes[i]); i += 1 }
        }
        return String(decoding: out, as: UTF8.self)
    }

    private static func stamp(_ date: Date) -> String {
        let seconds = date.timeIntervalSince1970
        var whole = time_t(seconds.rounded(.down)); var parts = tm()
        gmtime_r(&whole, &parts)
        let millis = min(Int((seconds - seconds.rounded(.down)) * 1000), 999)
        return String(format: "%04ld-%02ld-%02ldT%02ld:%02ld:%02ld.%03ldZ", Int(parts.tm_year) + 1900, Int(parts.tm_mon) + 1,
                      Int(parts.tm_mday), Int(parts.tm_hour), Int(parts.tm_min), Int(parts.tm_sec), millis)
    }

    private static func fileStamp(_ date: Date) -> String {
        stamp(date).replacingOccurrences(of: "-", with: "").replacingOccurrences(of: ":", with: "")
            .replacingOccurrences(of: ".", with: "").replacingOccurrences(of: "T", with: "-").replacingOccurrences(of: "Z", with: "")
    }

    /// The last line of the file as the previous launch left it. Raw facts only: a launch that
    /// ended in the background and was never seen again looks exactly like a Jetsam kill, and this
    /// does not claim to tell them apart.
    private static func summarisePriorLaunch(at path: String) -> LogFields? {
        let file = Darwin.open(path, O_RDONLY)
        guard file >= 0 else { return nil }
        defer { Darwin.close(file) }
        let size = Int(lseek(file, 0, SEEK_END))
        guard size > 0 else { return nil }
        let count = min(size, 8192)
        var buffer = [UInt8](repeating: 0, count: count)
        let got = pread(file, &buffer, count, off_t(size - count))
        guard got > 0 else { return nil }
        let text = String(decoding: buffer[0..<got], as: UTF8.self)
        guard let last = text.split(separator: "\n").last,
              let object = try? JSONSerialization.jsonObject(with: Data(last.utf8)) as? [String: Any] else {
            return ["prior_last_line_readable": false, "prior_file_bytes": .num(size)]
        }
        return ["prior_last_line_readable": true, "prior_launch": .text(object["launch"] as? String),
                "prior_last_ev": .text(object["ev"] as? String), "prior_last_ts": .text(object["ts"] as? String),
                "prior_file_bytes": .num(size)]
    }
}

/// What the build says it is, and what the device and process report about themselves.
/// All of it is thread-safe to read, so none of it is actor-isolated.
enum BuildInfo {
    static let unknown = "unknown"

    /// A commit is exactly 40 lowercase hex characters; anything else is not provenance.
    static func isCommit(_ value: String) -> Bool {
        value.utf8.count == 40 && value.utf8.allSatisfy { byte in
            switch byte { case 48...57, 97...102: return true; default: return false }
        }
    }

    /// The app's own commit, stamped into Info.plist at build time by Scripts/stamp-build-commit.sh.
    /// A key that is missing, empty, or still an unexpanded placeholder reads as `unknown`, never as a guess.
    static let app: String = {
        guard let raw = Bundle.main.object(forInfoDictionaryKey: "SCBuildCommit") as? String else { return unknown }
        let value = raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return isCommit(value) ? value : unknown
    }()

    /// The commit the native runtime says it was built from. This is what the control hello's
    /// `build` field carries; the laptop compares it against the other components.
    static var native: String {
        guard let pointer = sc_worker_build() else { return unknown }
        let value = String(cString: pointer).trimmingCharacters(in: .whitespacesAndNewlines)
        return value.isEmpty ? unknown : value
    }

    /// The same commit as the generated `runtimeBuildCommit` constant, kept as a cross-check: the
    /// constant is written by build_split_runtime.py into the Swift sources, `native` is read from
    /// the linked library. They should always agree.
    static var generated: String { runtimeBuildCommit }
}

enum Diagnostics {
    /// Bytes this process may still allocate before the system terminates it, from
    /// `os_proc_available_memory()`. This is headroom. It is not the device's RAM, and it is
    /// unavailable (nil) where the process has no limit, such as some simulators.
    static func headroomBytes() -> Int? {
        let value = Int(os_proc_available_memory())
        return value > 0 ? value : nil
    }

    static func thermalName(_ state: ProcessInfo.ThermalState) -> String {
        switch state {
        case .nominal: return "nominal"
        case .fair: return "fair"
        case .serious: return "serious"
        case .critical: return "critical"
        @unknown default: return "unknown"
        }
    }

    /// Memory headroom, thermal state and Low Power Mode: the three pressures that can silence a phone.
    static func pressure() -> LogFields {
        ["headroom_mb": .mib(headroomBytes()), "thermal": .text(thermalName(ProcessInfo.processInfo.thermalState)),
         "low_power": .bool(ProcessInfo.processInfo.isLowPowerModeEnabled)]
    }

    static func machineIdentifier() -> String {
        if let simulated = ProcessInfo.processInfo.environment["SIMULATOR_MODEL_IDENTIFIER"] { return "\(simulated) (simulator)" }
        var system = utsname(); uname(&system)
        return withUnsafeBytes(of: &system.machine) { raw in String(decoding: raw.prefix(while: { $0 != 0 }), as: UTF8.self) }
    }

    /// Everything the launch line records. `device_ram_mb` is the device's total physical memory;
    /// it is not headroom and must not be read as such. `headroom_mb` is the headroom.
    static func launchFields() -> LogFields {
        let version = ProcessInfo.processInfo.operatingSystemVersion
        #if targetEnvironment(simulator)
        let simulator = true
        #else
        let simulator = false
        #endif
        let native = BuildInfo.native
        let identity: LogFields = ["app_build": .text(BuildInfo.app), "native_build": .text(native),
                "generated_runtime_build": .text(BuildInfo.generated),
                "runtime_revision": .text(runtimeRevision), "native_revision": .text(String(cString: sc_worker_revision())),
                "ios": .text("\(version.majorVersion).\(version.minorVersion).\(version.patchVersion)"),
                "os_string": .text(ProcessInfo.processInfo.operatingSystemVersionString),
                "device": .text(machineIdentifier()), "simulator": .bool(simulator),
                "device_ram_mb": .mib(Int(clamping: ProcessInfo.processInfo.physicalMemory)),
                "pid": .num(ProcessInfo.processInfo.processIdentifier)]
        return identity + pressure()
    }
}

/// Lifecycle and pressure notifications: the reason the iPhone looked silent. Each handler only
/// records an event, so none of it can disturb a session.
///
/// Isolation: `install` is main-actor because the UIKit notification names are; the handlers it
/// registers are `@Sendable` closures that run on whatever thread posts (UIKit posts on main,
/// `ProcessInfo` may post on any). They touch only `EventLog.shared.event` and `Diagnostics`,
/// both nonisolated and thread-safe, and nothing main-actor-isolated.
@MainActor
enum LifecycleLog {
    private static var installed = false

    static func install() {
        guard !installed else { return }
        installed = true
        let center = NotificationCenter.default
        func watch(_ name: Notification.Name, _ event: String, flush: Bool = false) {
            // The returned token is dropped on purpose: the observer lives for the process.
            _ = center.addObserver(forName: name, object: nil, queue: nil) { _ in
                EventLog.shared.event(event, Diagnostics.pressure(), flush: flush)
            }
        }
        // flush: these are the moments the system can freeze or kill the process next.
        watch(UIApplication.willResignActiveNotification, "app_will_resign_active", flush: true)
        watch(UIApplication.didEnterBackgroundNotification, "app_did_enter_background", flush: true)
        watch(UIApplication.didReceiveMemoryWarningNotification, "memory_warning", flush: true)
        watch(UIApplication.willTerminateNotification, "app_will_terminate", flush: true)
        watch(UIApplication.protectedDataWillBecomeUnavailableNotification, "device_locking", flush: true)
        watch(UIApplication.didBecomeActiveNotification, "app_did_become_active")
        watch(UIApplication.willEnterForegroundNotification, "app_will_enter_foreground")
        watch(UIApplication.protectedDataDidBecomeAvailableNotification, "device_unlocked")
        watch(ProcessInfo.thermalStateDidChangeNotification, "thermal_state_changed", flush: true)
        watch(Notification.Name.NSProcessInfoPowerStateDidChange, "low_power_mode_changed")
    }
}
