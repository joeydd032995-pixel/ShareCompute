import Foundation

public enum RingLossReason: Equatable, Sendable, CustomStringConvertible {
    /// The node told us it was going away (iOS backgrounding, foreground service stopping,
    /// user-initiated stop). The good case: we found out while its socket still worked.
    case memberDeparted(NodeID)
    /// Heartbeats lapsed without notice — power loss, crash, cable pulled.
    case memberUnreachable(NodeID)

    public var departedNode: NodeID {
        switch self {
        case let .memberDeparted(node), let .memberUnreachable(node): return node
        }
    }

    public var description: String {
        switch self {
        case let .memberDeparted(node): return "\(node) left the ring"
        case let .memberUnreachable(node): return "\(node) stopped responding"
        }
    }
}

/// Why the ring ended up unrecoverable. Kept distinct from `RingLossReason`, which says *who* left:
/// this says why leaving proved fatal, which is a different question with a different fix.
public enum RingFailureCause: Equatable, Sendable, CustomStringConvertible {
    /// Re-formation was attempted and did not succeed.
    case reformationFailed(String)
    /// The host never reported an outcome within `reformationTimeout`.
    case reformationTimedOut
    /// Re-formation failed `maxReformationAttempts` times. Carries the last error, because the
    /// count alone tells nobody which handle refused to release.
    case reformationExhausted(attempts: Int, lastError: String)

    public var description: String {
        switch self {
        case let .reformationFailed(detail):
            return "the ring could not be rebuilt — \(detail)"
        case .reformationTimedOut:
            return "rebuilding the ring timed out"
        case let .reformationExhausted(attempts, lastError):
            return "rebuilding the ring failed \(attempts) times — \(lastError)"
        }
    }
}

public enum RingHealth: Equatable, Sendable {
    case healthy
    /// No progress, but membership is intact — most likely just slow. Never escalates on its own.
    case stalled(since: Date)
    /// A member is gone and the ring is being rebuilt at a new epoch. Generation has stopped, but
    /// this is **recoverable**: the host tears the MLX group down and re-initialises it.
    ///
    /// Reaching this state at all depends on `Patches/mlx/0002` — before `finalize()` existed there
    /// was no teardown, so loss went straight to `.lost`.
    case reforming(RingLossReason, since: Date)
    /// Terminal. Either re-formation failed, or it was never attempted.
    case lost(RingLossReason, RingFailureCause?)
}

public struct RingWatchdogConfig: Sendable, Equatable {
    /// How long without a token before reporting a stall. Generous, because a large prefill on a
    /// phone legitimately takes many seconds and must not be mistaken for a failure.
    public var stallThreshold: TimeInterval
    /// How long to wait after learning a member is gone before declaring the ring lost. Short,
    /// because the diagnosis is already known — this only allows for tokens already in flight.
    public var graceAfterLoss: TimeInterval
    /// How long the host may spend rebuilding before the ring is called lost.
    ///
    /// Load-bearing. Re-formation involves releasing the model, `finalize()`, and a fresh
    /// `DistributedGroup.initialize`, any of which can block — and a host that never reports back
    /// would leave the watchdog waiting forever, which is precisely the hang this whole project
    /// exists to eliminate. An unbounded wait is not a recoverable state, it is the original bug.
    public var reformationTimeout: TimeInterval
    /// How many times to rebuild before giving up. Repeated failure usually means a handle the
    /// teardown cannot release (F14), and retrying without releasing it fails identically.
    public var maxReformationAttempts: Int

    public init(
        stallThreshold: TimeInterval = 10,
        graceAfterLoss: TimeInterval = 2,
        reformationTimeout: TimeInterval = 30,
        maxReformationAttempts: Int = 3
    ) {
        self.stallThreshold = stallThreshold
        self.graceAfterLoss = graceAfterLoss
        self.reformationTimeout = reformationTimeout
        self.maxReformationAttempts = maxReformationAttempts
    }

    public static let `default` = RingWatchdogConfig()
}

/// Decides when an in-flight generation has become unrecoverable.
///
/// This exists because of a hard limitation, not a design preference. A departing rank left the
/// survivors blocked inside MLX's ring backend on a `std::promise` that was never fulfilled and
/// never broken, with no exception thrown and no socket timeout (`findings.md`, Spike B); and the
/// group could not be rebuilt, because `distributed::init` cached it in a process-lifetime static
/// (Spike A). Both are now patched — `Patches/mlx/0001` and `0002` — which is what makes the
/// re-formation below possible at all. Neither patch has ever run on hardware.
///
/// **Stage 3 changed what this type is for.** Before `Patches/mlx/0002`, loss was terminal because
/// no teardown existed, and the achievable goal was only to convert an indefinite silent hang into
/// a reported failure. `finalize()` now exists, so confirmed loss moves to `.reforming` and the host
/// rebuilds the group at a new epoch. Terminal is now an *outcome* of re-formation, not its
/// precondition.
///
/// The discriminating rule is unchanged and still the important one: a stall alone is *never* enough
/// to declare loss — only a stall combined with known membership loss is. Slow is not the same as
/// dead, and treating them alike would abort perfectly healthy long prefills.
///
/// Re-formation is bounded on both axes, by time and by attempts. An unbounded retry is not
/// resilience; it is the original hang wearing a different name.
public final class RingWatchdog {
    public let config: RingWatchdogConfig

    private enum Phase: Equatable {
        case running
        case reforming(reason: RingLossReason, since: Date)
        case terminal(reason: RingLossReason, cause: RingFailureCause?)
    }

    private var lastProgressAt: Date
    private var pendingLoss: (reason: RingLossReason, recordedAt: Date)?
    private var phase: Phase = .running
    private var reformationAttempts = 0

    public init(config: RingWatchdogConfig = .default, startedAt: Date) {
        self.config = config
        self.lastProgressAt = startedAt
    }

    /// The ring is unrecoverable and no later request can succeed.
    ///
    /// Reflects loss that has been *confirmed*, and — since Stage 3 — that re-formation has failed,
    /// timed out or been exhausted. Confirmation happens in `evaluate(at:)` and
    /// `beginGeneration(at:)`, both of which need a timestamp.
    public var isTerminal: Bool {
        if case .terminal = phase { return true }
        return false
    }

    /// The host should tear the group down and re-initialise. Bounded by `reformationTimeout`.
    public var isReforming: Bool {
        if case .reforming = phase { return true }
        return false
    }

    /// Which node broke the ring, whether or not that has proved fatal yet.
    public var lossReason: RingLossReason? {
        switch phase {
        case .running: return nil
        case let .reforming(reason, _): return reason
        case let .terminal(reason, _): return reason
        }
    }

    /// Why re-formation did not save it. `nil` while recoverable.
    public var failureCause: RingFailureCause? {
        if case let .terminal(_, cause) = phase { return cause }
        return nil
    }

    /// How many rebuilds have been attempted for the current loss.
    public var attemptCount: Int { reformationAttempts }

    /// A token (or any forward progress) arrived.
    public func recordProgress(at now: Date) {
        guard case .running = phase else { return }
        lastProgressAt = now
    }

    /// Membership told us a node is gone. Does not by itself end the generation — tokens already
    /// in flight may still land.
    public func recordLoss(_ reason: RingLossReason, at now: Date) {
        guard case .running = phase else { return }
        // Keep the first diagnosis: it names the node that actually broke the ring.
        guard pendingLoss == nil else { return }
        pendingLoss = (reason, now)
    }

    // MARK: - Re-formation, reported by the host

    /// The host finished rebuilding: `finalize()` returned `true` and a fresh group initialised.
    ///
    /// Clears the loss entirely. The next `beginGeneration` is admitted, because the ring genuinely
    /// is a new one — a new epoch with the departed member gone from it.
    public func reformationSucceeded(at now: Date) {
        guard case .reforming = phase else { return }
        phase = .running
        pendingLoss = nil
        reformationAttempts = 0
        lastProgressAt = now
    }

    /// The host cannot rebuild at all — the capability is absent from this build, not merely
    /// failing. Terminal immediately.
    ///
    /// Distinct from `reformationFailed` because retrying is pointless: nothing about the situation
    /// will differ on a second attempt, so burning the attempt budget on identical failures would
    /// only delay an answer that is already known. The concrete case is an app built against an MLX
    /// without `Patches/mlx-swift/0001`, where `DistributedGroup.finalize()` does not exist.
    public func reformationUnavailable(_ detail: String, at now: Date) {
        guard case let .reforming(reason, _) = phase else { return }
        phase = .terminal(reason: reason, cause: .reformationFailed(detail))
    }

    /// The host tried and failed — most often `finalize()` returning `false` because a handle
    /// outlived the teardown (F14).
    ///
    /// Terminal once `maxReformationAttempts` is reached. Below that the watchdog stays in
    /// `.reforming` and the host may try again, with the clock restarted so a slow retry is not
    /// punished for the previous attempt's time.
    public func reformationFailed(_ detail: String, at now: Date) {
        guard case let .reforming(reason, _) = phase else { return }
        reformationAttempts += 1
        if reformationAttempts >= config.maxReformationAttempts {
            phase = .terminal(
                reason: reason,
                cause: .reformationExhausted(attempts: reformationAttempts, lastError: detail)
            )
        } else {
            phase = .reforming(reason: reason, since: now)
        }
    }

    /// Convenience: feed `MembershipService` output straight in.
    public func consume(_ events: [MembershipEvent], at now: Date) {
        for event in events {
            switch event {
            case let .nodeDraining(node):
                recordLoss(.memberDeparted(node), at: now)
            case let .nodeEvicted(node, reason):
                switch reason {
                case .drained:
                    recordLoss(.memberDeparted(node), at: now)
                case .heartbeatLost, .leaseExpired:
                    recordLoss(.memberUnreachable(node), at: now)
                }
            case .nodeJoined, .epochChanged, .epochChangeDeferred:
                continue
            }
        }
    }

    @discardableResult
    public func evaluate(at now: Date) -> RingHealth {
        switch phase {
        case let .terminal(reason, cause):
            return .lost(reason, cause)

        case let .reforming(reason, since):
            // The host is rebuilding. Bound it: a host that never reports back must not leave the
            // ring waiting indefinitely, which is the failure mode this project exists to remove.
            if now.timeIntervalSince(since) >= config.reformationTimeout {
                phase = .terminal(reason: reason, cause: .reformationTimedOut)
                return .lost(reason, .reformationTimedOut)
            }
            return .reforming(reason, since: since)

        case .running:
            break
        }

        if let pending = pendingLoss {
            // Each token still arriving pushes the deadline out, so a generation that is genuinely
            // still producing is never cut short.
            let deadlineFrom = max(lastProgressAt, pending.recordedAt)
            if now.timeIntervalSince(deadlineFrom) >= config.graceAfterLoss {
                // Stage 3: confirmed loss is now the *start* of re-formation, not the end of the
                // ring. `finalize()` exists, so the host has something to do here.
                phase = .reforming(reason: pending.reason, since: now)
                return .reforming(pending.reason, since: now)
            }
            return .stalled(since: lastProgressAt)
        }

        if now.timeIntervalSince(lastProgressAt) >= config.stallThreshold {
            return .stalled(since: lastProgressAt)
        }

        return .healthy
    }

    /// Begins a new request. Returns `false` while the ring is unrecoverable *or* mid-rebuild, so
    /// callers fail fast instead of issuing work into a group that is being torn down.
    public func beginGeneration(at now: Date) -> Bool {
        // Confirm any overdue loss first. Without this, a caller that recorded a loss and then
        // started a request without evaluating in between would be admitted into a dead group,
        // where the request could only hang — the exact failure this type exists to prevent.
        evaluate(at: now)
        switch phase {
        case .terminal, .reforming:
            return false
        case .running:
            lastProgressAt = now
            pendingLoss = nil
            return true
        }
    }
}

extension RingHealth {
    /// Message for the user. Names the device, and never promises recovery it cannot deliver.
    ///
    /// Before Stage 3 this said a restart was the only option, which was true then and is not now.
    /// It still says so when re-formation has actually failed — a lie in *that* direction, telling
    /// someone the ring is fine when it is dead, is the worse one.
    public var userFacingMessage: String? {
        switch self {
        case .healthy:
            return nil
        case .stalled:
            return "Still working — this can take a while on large prompts."
        case let .reforming(reason, _):
            return "\(reason.description). Rebuilding the ring without it…"
        case let .lost(reason, cause):
            guard let cause else {
                return "\(reason.description). This ring can't be rebuilt without restarting the app."
            }
            return "\(reason.description) — \(cause.description). Restart the app to try again."
        }
    }

    /// Whether the app should keep the user waiting. `stalled` and `reforming` are both live states;
    /// only `lost` is an ending.
    public var isRecoverable: Bool {
        if case .lost = self { return false }
        return true
    }
}
