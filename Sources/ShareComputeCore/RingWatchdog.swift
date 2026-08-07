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

public enum RingHealth: Equatable, Sendable {
    case healthy
    /// No progress, but membership is intact — most likely just slow. Never escalates on its own.
    case stalled(since: Date)
    /// A member is gone and generation has stopped. Terminal for this process.
    case lost(RingLossReason)
}

public struct RingWatchdogConfig: Sendable, Equatable {
    /// How long without a token before reporting a stall. Generous, because a large prefill on a
    /// phone legitimately takes many seconds and must not be mistaken for a failure.
    public var stallThreshold: TimeInterval
    /// How long to wait after learning a member is gone before declaring the ring lost. Short,
    /// because the diagnosis is already known — this only allows for tokens already in flight.
    public var graceAfterLoss: TimeInterval

    public init(stallThreshold: TimeInterval = 10, graceAfterLoss: TimeInterval = 2) {
        self.stallThreshold = stallThreshold
        self.graceAfterLoss = graceAfterLoss
    }

    public static let `default` = RingWatchdogConfig()
}

/// Decides when an in-flight generation has become unrecoverable.
///
/// This exists because of a hard limitation, not a design preference. A departing rank leaves the
/// survivors blocked inside MLX's ring backend on a `std::promise` that is never fulfilled and
/// never broken, with no exception thrown and no socket timeout (`findings.md`, Spike B). The
/// blocked thread cannot be freed, and the group cannot be rebuilt because
/// `distributed::init` caches it in a process-lifetime static (Spike A).
///
/// So the watchdog does not recover anything. What it does is convert **an indefinite silent hang
/// into a reported failure**: the UI stops waiting, the user is told which device left, and the
/// app can say plainly that a restart is required. That is the whole of what is achievable
/// without patching MLX, and it is strictly better than the current behaviour.
///
/// The discriminating rule is that a stall alone is *never* enough to declare loss — only a stall
/// combined with known membership loss is. Slow is not the same as dead, and treating them alike
/// would abort perfectly healthy long prefills.
public final class RingWatchdog {
    public let config: RingWatchdogConfig

    private var lastProgressAt: Date
    private var pendingLoss: (reason: RingLossReason, recordedAt: Date)?
    private var terminalLoss: RingLossReason?

    public init(config: RingWatchdogConfig = .default, startedAt: Date) {
        self.config = config
        self.lastProgressAt = startedAt
    }

    /// Once the ring is lost it stays lost for the lifetime of the process: the MLX group cannot
    /// be rebuilt, so no later request can succeed either.
    ///
    /// Reflects loss that has been *confirmed*. Confirmation happens in `evaluate(at:)` and
    /// `beginGeneration(at:)`, both of which need a timestamp; a loss recorded but not yet past
    /// its grace window is not terminal yet.
    public var isTerminal: Bool { terminalLoss != nil }

    public var lossReason: RingLossReason? { terminalLoss }

    /// A token (or any forward progress) arrived.
    public func recordProgress(at now: Date) {
        guard terminalLoss == nil else { return }
        lastProgressAt = now
    }

    /// Membership told us a node is gone. Does not by itself end the generation — tokens already
    /// in flight may still land.
    public func recordLoss(_ reason: RingLossReason, at now: Date) {
        guard terminalLoss == nil else { return }
        // Keep the first diagnosis: it names the node that actually broke the ring.
        guard pendingLoss == nil else { return }
        pendingLoss = (reason, now)
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
        if let terminalLoss { return .lost(terminalLoss) }

        if let pending = pendingLoss {
            // Each token still arriving pushes the deadline out, so a generation that is genuinely
            // still producing is never cut short.
            let deadlineFrom = max(lastProgressAt, pending.recordedAt)
            if now.timeIntervalSince(deadlineFrom) >= config.graceAfterLoss {
                terminalLoss = pending.reason
                return .lost(pending.reason)
            }
            return .stalled(since: lastProgressAt)
        }

        if now.timeIntervalSince(lastProgressAt) >= config.stallThreshold {
            return .stalled(since: lastProgressAt)
        }

        return .healthy
    }

    /// Begins a new request. Returns `false` when the ring is already unrecoverable, so callers
    /// fail fast instead of issuing work that can only hang.
    public func beginGeneration(at now: Date) -> Bool {
        // Confirm any overdue loss first. Without this, a caller that recorded a loss and then
        // started a request without evaluating in between would be admitted into a dead group,
        // where the request could only hang — the exact failure this type exists to prevent.
        evaluate(at: now)
        guard terminalLoss == nil else { return false }
        lastProgressAt = now
        pendingLoss = nil
        return true
    }
}

extension RingHealth {
    /// Message for the user. Names the device, and is honest that a restart is the only recovery —
    /// the MLX group cannot be rebuilt in-process.
    public var userFacingMessage: String? {
        switch self {
        case .healthy:
            return nil
        case .stalled:
            return "Still working — this can take a while on large prompts."
        case let .lost(reason):
            return "\(reason.description). This ring can't be rebuilt without restarting the app."
        }
    }
}
