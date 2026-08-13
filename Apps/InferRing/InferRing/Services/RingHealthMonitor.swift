//
import Foundation
import Observation
import ShareComputeCore
// For MLXManager.canReform and RingError, both in the Ring framework target. Added with the
// re-formation gate — without it `canReform` is an unresolved name, and the compiler's message
// points at the use site rather than the missing import (findings.md F18).
import Ring
#if os(iOS)
import UIKit
#endif

/// Watches ring membership during and between generations and reports when the ring has become
/// unusable.
///
/// ## Why this only reports, and never repairs
///
/// A departing rank leaves survivors blocked inside MLX's ring backend on a `std::promise` that is
/// never fulfilled and never broken — no exception is thrown, and the sockets are non-blocking
/// with no receive timeout. The blocked thread cannot be freed. Rebuilding the group is also
/// impossible, because `distributed::init` caches it in a process-lifetime static and returns the
/// stale group on every later call, ignoring the rewritten hostfile.
///
/// See `findings.md` (Spikes A and B) for the source references.
///
/// So the goal here is narrow and achievable: turn **an indefinite silent hang into a reported
/// failure** that names the device which left and tells the user a restart is needed. Seamless
/// re-formation requires patching the MLX fork and is out of scope.
@MainActor
@Observable
final class RingHealthMonitor {

    @ObservationIgnored
    @Inject
    private var coordinator: RingCoordinator?

    @ObservationIgnored
    @Inject
    private var hardwareMonitor: HardwareMonitor?

    /// Current assessment. Drives the UI banner.
    private(set) var health: RingHealth = .healthy

    var isRingLost: Bool {
        if case .lost = health { return true }
        return false
    }

    var statusMessage: String? { health.userFacingMessage }

    @ObservationIgnored
    private let clock = SystemClock()

    @ObservationIgnored
    private lazy var membership = MembershipService(
        localNodeID: NodeID(ServiceInfo.bonjourName),
        clock: clock,
        config: MembershipConfig(
            leaseDuration: 60,
            heartbeatInterval: 2,
            missedHeartbeatsBeforeEviction: 3,
            minimumEpochDwellTime: 5
        )
    )

    @ObservationIgnored
    private lazy var watchdog = RingWatchdog(startedAt: clock.now)

    @ObservationIgnored
    private var heartbeatTask: Task<Void, Never>?

    // MARK: - Lifecycle

    func start() {
        stop()
        publishLocalProfile()
        heartbeatTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.probePeers()
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
        observeAppLifecycle()
    }

    func stop() {
        heartbeatTask?.cancel()
        heartbeatTask = nil
    }

    // MARK: - Generation hooks, called by ModelManager

    /// Returns false when the ring is already dead, so a request fails immediately instead of
    /// being issued into a group where it could only hang.
    func beginGeneration() -> Bool {
        let allowed = watchdog.beginGeneration(at: clock.now)
        health = watchdog.evaluate(at: clock.now)
        return allowed
    }

    /// A token arrived. Resets the stall deadline.
    func recordProgress() {
        watchdog.recordProgress(at: clock.now)
    }

    /// Current verdict, refreshed. `ModelManager` polls this while streaming so it can abandon a
    /// generation that can never complete.
    @discardableResult
    func refreshHealth() -> RingHealth {
        health = watchdog.evaluate(at: clock.now)
        if case .reforming = health { startReformationIfNeeded() }
        return health
    }

    // MARK: - Re-formation

    @ObservationIgnored
    private var reformationTask: Task<Void, Never>?

    /// Drives one rebuild, and reports the outcome back to the watchdog.
    ///
    /// Guarded against re-entry: `refreshHealth()` is polled once a second during a generation, and
    /// launching a second teardown while the first is mid-`finalize()` would race two rebuilds
    /// against the same group.
    ///
    /// Every path reports. A rebuild that finishes without telling the watchdog would sit in
    /// `.reforming` until the timeout fired — recoverable, but it would look like a hang for
    /// however long that took, which is the failure this project exists to remove.
    private func startReformationIfNeeded() {
        guard reformationTask == nil else { return }

        // Ask before trying. Against stock MLX the teardown does not exist, and three rapid
        // identical failures would only delay an answer already known at compile time.
        guard MLXManager.canReform else {
            watchdog.reformationUnavailable(
                RingError.finalizeUnavailable.errorDescription ?? "teardown unavailable",
                at: clock.now
            )
            health = watchdog.evaluate(at: clock.now)
            return
        }

        reformationTask = Task { [weak self] in
            guard let self else { return }
            do {
                try await self.coordinator?.reformRing()
                self.watchdog.reformationSucceeded(at: self.clock.now)
                dprint("Ring re-formed at a new epoch")
            }
            catch {
                // Includes RingError.handlesOutlivedTeardown, which is the F14 failure: something
                // still holds the group. Retrying without releasing it fails identically, so the
                // detail matters more than the attempt count.
                self.watchdog.reformationFailed(
                    error.localizedDescription, at: self.clock.now
                )
                dprint("Ring re-formation failed: \(error.localizedDescription)")
            }
            self.reformationTask = nil
            self.health = self.watchdog.evaluate(at: self.clock.now)
        }
    }

    // MARK: - Peer probing

    private func publishLocalProfile() {
        guard let profile = localCapabilityProfile() else { return }
        membership.nodeAppeared(profile: profile)
    }

    private func probePeers() async {
        guard let coordinator else { return }

        publishLocalProfile()

        for peer in coordinator.ringPeers {
            // Reuses the /ping endpoint and DataClient.ping(), both of which already existed but
            // were never called from anywhere — there was no liveness detection at all.
            let alive = await peer.client.ping()
            let nodeID = NodeID(peer.device.name)

            if alive {
                if let profile = capabilityProfile(for: peer) {
                    membership.nodeAppeared(profile: profile)
                }
                membership.heartbeatSucceeded(from: nodeID)
            }
            else {
                membership.heartbeatFailed(for: nodeID)
            }
        }

        let events = membership.tick(at: clock.now)
        if !events.isEmpty {
            dprint("Ring membership events: \(events)")
        }
        watchdog.consume(events, at: clock.now)
        refreshHealth()
    }

    // MARK: - Inbound announcements

    /// A peer told us it is going away. Handled eagerly: this is the one path where we learn about
    /// a departure while the peer's socket still works, which is the difference between a reported
    /// failure and an indefinite hang.
    func peerAnnouncedDrain(_ nodeName: String) {
        let events = membership.nodeAnnouncedDrain(NodeID(nodeName))
        watchdog.consume(events, at: clock.now)
        refreshHealth()
    }

    // MARK: - Local lifecycle

    private func observeAppLifecycle() {
        #if os(iOS)
        NotificationCenter.default.addObserver(
            forName: UIApplication.willResignActiveNotification,
            object: nil,
            queue: .main
        ) { _ in
            MainActor.assumeIsolated {
                // Two things are load-bearing on this one line.
                //
                // Explicit generic arguments, because a bare `Task { }` is ambiguous between
                // Task.init(priority:operation:) and Task.init(name:priority:operation:) — every
                // leading parameter of both has a default, so a trailing closure alone picks
                // neither. announceLocalDrain() is async and non-throwing, hence <Void, Never>.
                //
                // And the `_ =`, because assumeIsolated is generic over its closure's result: a
                // single-expression closure would implicitly return the Task, inferring
                // T == Task<Void, Never> where this context wants Void. Discarding makes the body
                // a statement. Discarding is also correct on its own terms — the drain is
                // deliberately fire-and-forget, since awaiting a peer during willResignActive
                // risks suspension having told nobody.
                _ = Task<Void, Never> { await RingHealthMonitor.announceLocalDrain() }
            }
        }
        #endif
    }

    /// Tells every peer we are leaving, before the OS tears our sockets down.
    ///
    /// Deliberately fire-and-forget with a short timeout: `willResignActive` grants only a brief
    /// window, and blocking on a peer's reply risks being suspended mid-await having told nobody.
    static func announceLocalDrain() async {
        @Inject var coordinator: RingCoordinator?
        guard let peers = coordinator?.ringPeers else { return }

        let request = DrainRequest(
            nodeName: ServiceInfo.bonjourName,
            requestID: UUID().uuidString,
            timestamp: Date()
        )

        await withTaskGroup(of: Void.self) { group in
            for peer in peers {
                group.addTask {
                    await peer.client.announceDrain(request: request)
                }
            }
        }
    }

    // MARK: - Profiles

    private func localCapabilityProfile() -> CapabilityProfile? {
        guard let profile = hardwareMonitor?.currentProfile else { return nil }
        return CapabilityProfile.from(profile, nodeID: NodeID(ServiceInfo.bonjourName))
    }

    private func capabilityProfile(for peer: RingDevice) -> CapabilityProfile? {
        guard let profile = peer.device.hardwareProfile else { return nil }
        return CapabilityProfile.from(profile, nodeID: NodeID(peer.device.name))
    }
}

extension CapabilityProfile {
    /// Bridges the app's existing `HardwareProfile` into the core's capability model.
    ///
    /// `recommendedUsageRAM` is used as `usableBytes` because it is already discounted for iOS in
    /// `HardwareProfile.from(gpuInfo:)` — the watchdog there kills at the OS-reported figure.
    static func from(_ profile: HardwareProfile, nodeID: NodeID) -> CapabilityProfile {
        let isDesktop = profile.idiom == .mac

        return CapabilityProfile(
            nodeID: nodeID,
            connectivity: isDesktop ? .coreDesktop : .elasticMobile,
            backgroundLink: isDesktop ? .desktopUnrestricted : .iosSuspended,
            memory: MemoryProfile(
                totalBytes: profile.totalRAM,
                usableBytes: profile.recommendedUsageRAM,
                reclaimModel: isDesktop ? .appleUnifiedMemoryPressure : .iosJetsam
            ),
            runtimeBackends: [.mlxMetal],
            power: PowerProfile(
                isWallPowered: isDesktop,
                batteryFraction: nil,
                thermalState: PowerProfile.ThermalState(processInfo: ProcessInfo.processInfo)
            ),
            trust: .trustedCore,
            // iOS keeps this despite spec §12.1 forbidding required stages on mobile: pooling an
            // iPhone's RAM with a Mac's is the product. Safety comes from the short OS-clamped
            // lease and drain-on-background instead.
            declaresCanHostRequiredStage: true
        )
    }
}

extension PowerProfile.ThermalState {
    init(processInfo: ProcessInfo) {
        switch processInfo.thermalState {
        case .nominal: self = .nominal
        case .fair: self = .fair
        case .serious: self = .serious
        case .critical: self = .critical
        @unknown default: self = .nominal
        }
    }
}
