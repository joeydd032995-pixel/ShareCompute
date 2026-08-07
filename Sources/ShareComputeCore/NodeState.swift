import Foundation

/// Lifecycle state of a node with respect to the ring. Spec §10.2.
public enum NodeState: String, Codable, Sendable, CaseIterable {
    /// Desktop node hosting required stages; expected stable across the SLA window.
    case activeCore
    /// Android foreground service, or iOS while foregrounded. Stable only while the lease holds.
    case activeElastic
    /// Short bursts only. Never holds required stages.
    case opportunistic
    /// Announced departure, handing work back. Spec §10.3 names a drain protocol but gives it no
    /// state; without one, "leaving" and "already gone" are indistinguishable to the coordinator,
    /// which is exactly the ambiguity that causes the current deadlock.
    case draining
    /// Not participating. Ineligible for new work.
    case suspended

    /// Whether a node in this state may hold prefill / authoritative decode / KV store.
    ///
    /// `activeElastic` is included deliberately. Spec §12.1 forbids required stages on mobile,
    /// but infer-ring exists to combine an iPhone's RAM with a Mac's — applying the spec
    /// literally would delete the product's headline feature. The safety the spec is reaching
    /// for is provided instead by short leases (`CapabilityProfile.maximumSupportableLeaseDuration`)
    /// and mandatory drain-on-background.
    public var canHostRequiredStage: Bool {
        switch self {
        case .activeCore, .activeElastic:
            return true
        case .opportunistic, .draining, .suspended:
            return false
        }
    }

    /// Whether the node should be counted when planning the next epoch's shards.
    public var participatesInPlanning: Bool {
        switch self {
        case .activeCore, .activeElastic, .opportunistic:
            return true
        case .draining, .suspended:
            return false
        }
    }
}

/// Something that happens to a node and may move it between states.
public enum NodeEvent: String, Codable, Sendable, CaseIterable {
    case joinedAsCore
    case joinedAsElastic
    case joinedAsOpportunistic
    /// The OS is about to stop us being frontmost (iOS `willResignActive`, foreground service
    /// stopping). This is the event that turns today's deadlock into a planned departure.
    case enteredBackground
    case returnedToForeground
    /// The coordinator asked us to hand work back, typically to re-plan the ring.
    case drainRequested
    /// We finished handing work back.
    case drainCompleted
    case leaseExpired
    case heartbeatLost
    /// Voluntary demotion under thermal or battery duress. Spec §10.3.
    case powerDuress
}

/// Transitions expressed as data so they can be exhaustively tested without a network, a clock,
/// or a device. Spec §10.3.
public enum NodeStateMachine {
    /// The next state, or `nil` if the event is not meaningful in that state.
    ///
    /// Returning `nil` rather than silently staying put matters: an unexpected transition is a
    /// bug worth surfacing, not something to absorb.
    public static func next(from state: NodeState, on event: NodeEvent) -> NodeState? {
        switch (state, event) {

        // Joining, always from suspended.
        case (.suspended, .joinedAsCore): return .activeCore
        case (.suspended, .joinedAsElastic): return .activeElastic
        case (.suspended, .joinedAsOpportunistic): return .opportunistic

        // A returning node re-syncs as opportunistic and is promoted only after the coordinator
        // has seen a fresh capability profile. Promoting straight back to its old role would
        // trust stale information about a device whose battery, thermal state and network may
        // all have changed while it was away.
        case (.suspended, .returnedToForeground): return .opportunistic

        // Backgrounding always drains first. This is the whole point of the milestone: announce
        // departure while the socket still works, rather than vanishing mid-collective.
        case (.activeCore, .enteredBackground),
             (.activeElastic, .enteredBackground),
             (.opportunistic, .enteredBackground):
            return .draining

        // Coordinator-initiated drain, e.g. to re-plan shards for a new epoch.
        case (.activeCore, .drainRequested),
             (.activeElastic, .drainRequested),
             (.opportunistic, .drainRequested):
            return .draining

        // Power duress: elastic nodes step down a rung; an opportunistic node leaves outright.
        // A core node also drains — a laptop that is overheating while holding a required stage
        // should hand it off deliberately rather than be killed holding it.
        case (.activeElastic, .powerDuress): return .opportunistic
        case (.activeCore, .powerDuress): return .draining
        case (.opportunistic, .powerDuress): return .draining

        // Terminal paths into suspension.
        case (.draining, .drainCompleted),
             (.draining, .leaseExpired),
             (.draining, .heartbeatLost):
            return .suspended

        case (.activeCore, .leaseExpired),
             (.activeElastic, .leaseExpired),
             (.opportunistic, .leaseExpired),
             (.activeCore, .heartbeatLost),
             (.activeElastic, .heartbeatLost),
             (.opportunistic, .heartbeatLost):
            return .suspended

        // Everything else is not a legal transition. Notably a draining node cannot be rescued by
        // returning to the foreground: the coordinator may already be re-planning around its
        // departure, and reversing that mid-flight would race.
        default:
            return nil
        }
    }

    /// Applies an event, leaving the state untouched when the transition is not legal.
    /// Returns whether the state actually moved.
    @discardableResult
    public static func apply(_ event: NodeEvent, to state: inout NodeState) -> Bool {
        guard let newState = next(from: state, on: event) else { return false }
        state = newState
        return true
    }

    /// The state a node should enter when it first joins, given what it declares about itself.
    public static func initialState(for profile: CapabilityProfile) -> NodeState {
        guard profile.declaresCanHostRequiredStage else { return .opportunistic }
        switch profile.connectivity {
        case .coreDesktop: return .activeCore
        case .elasticMobile: return .activeElastic
        case .opportunisticMobile: return .opportunistic
        }
    }
}
