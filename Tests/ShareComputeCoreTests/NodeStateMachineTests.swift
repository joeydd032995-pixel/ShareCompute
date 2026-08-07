import XCTest
@testable import ShareComputeCore

final class NodeStateMachineTests: XCTestCase {

    // MARK: - The transition that matters most

    /// The failure this whole milestone exists to fix: an iOS device being backgrounded while it
    /// holds a pipeline stage. It must announce departure rather than vanish.
    func testBackgroundingAlwaysDrainsFirstAndNeverJumpsStraightToSuspended() {
        for state in [NodeState.activeCore, .activeElastic, .opportunistic] {
            XCTAssertEqual(
                NodeStateMachine.next(from: state, on: .enteredBackground),
                .draining,
                "\(state) must drain before leaving, not disappear"
            )
        }
    }

    func testDrainingCompletesToSuspended() {
        XCTAssertEqual(NodeStateMachine.next(from: .draining, on: .drainCompleted), .suspended)
    }

    /// A node that is already draining must not be pulled back into service: the coordinator may
    /// already be re-planning around its departure.
    func testDrainingCannotBeCancelledByReturningToForeground() {
        XCTAssertNil(NodeStateMachine.next(from: .draining, on: .returnedToForeground))
    }

    // MARK: - Rejoining

    func testReturningNodeRejoinsAsOpportunisticRatherThanItsOldRole() {
        XCTAssertEqual(
            NodeStateMachine.next(from: .suspended, on: .returnedToForeground),
            .opportunistic,
            "a returning node must be re-profiled before it is trusted with required stages again"
        )
    }

    func testJoinPathsFromSuspended() {
        XCTAssertEqual(NodeStateMachine.next(from: .suspended, on: .joinedAsCore), .activeCore)
        XCTAssertEqual(NodeStateMachine.next(from: .suspended, on: .joinedAsElastic), .activeElastic)
        XCTAssertEqual(NodeStateMachine.next(from: .suspended, on: .joinedAsOpportunistic), .opportunistic)
    }

    func testCannotJoinFromAnAlreadyActiveState() {
        XCTAssertNil(NodeStateMachine.next(from: .activeCore, on: .joinedAsCore))
        XCTAssertNil(NodeStateMachine.next(from: .activeElastic, on: .joinedAsElastic))
    }

    // MARK: - Loss

    func testLeaseExpiryAndHeartbeatLossAlwaysReachSuspended() {
        for state in [NodeState.activeCore, .activeElastic, .opportunistic, .draining] {
            XCTAssertEqual(NodeStateMachine.next(from: state, on: .leaseExpired), .suspended)
            XCTAssertEqual(NodeStateMachine.next(from: state, on: .heartbeatLost), .suspended)
        }
    }

    // MARK: - Power duress (spec §10.3)

    func testPowerDuressDemotesElasticButEjectsOpportunistic() {
        XCTAssertEqual(NodeStateMachine.next(from: .activeElastic, on: .powerDuress), .opportunistic)
        XCTAssertEqual(NodeStateMachine.next(from: .opportunistic, on: .powerDuress), .draining)
    }

    /// An overheating core node holding a required stage should hand it off deliberately rather
    /// than be killed still holding it.
    func testPowerDuressDrainsCoreNodeRatherThanLeavingItHoldingRequiredWork() {
        XCTAssertEqual(NodeStateMachine.next(from: .activeCore, on: .powerDuress), .draining)
    }

    // MARK: - Whole-table invariants

    /// Every reachable path must terminate in `suspended` rather than stranding a node in a state
    /// with no way out. Exhaustive over the state x event product.
    func testNoStateIsATrapAndSuspendedIsAlwaysReachable() {
        for state in NodeState.allCases {
            var reachable: Set<NodeState> = [state]
            var frontier = [state]
            while let current = frontier.popLast() {
                for event in NodeEvent.allCases {
                    guard let next = NodeStateMachine.next(from: current, on: event) else { continue }
                    if reachable.insert(next).inserted {
                        frontier.append(next)
                    }
                }
            }
            XCTAssertTrue(
                reachable.contains(.suspended),
                "\(state) cannot reach .suspended — a node in it could never be cleanly evicted"
            )
        }
    }

    /// Only states that can actually be depended upon may hold required stages.
    func testOnlyActiveStatesMayHoldRequiredStages() {
        XCTAssertTrue(NodeState.activeCore.canHostRequiredStage)
        XCTAssertTrue(NodeState.activeElastic.canHostRequiredStage)
        XCTAssertFalse(NodeState.opportunistic.canHostRequiredStage)
        XCTAssertFalse(NodeState.draining.canHostRequiredStage)
        XCTAssertFalse(NodeState.suspended.canHostRequiredStage)
    }

    /// A draining node must be excluded from the next epoch's plan — including it is what would
    /// re-introduce the stall.
    func testDrainingAndSuspendedNodesAreExcludedFromPlanning() {
        XCTAssertFalse(NodeState.draining.participatesInPlanning)
        XCTAssertFalse(NodeState.suspended.participatesInPlanning)
        XCTAssertTrue(NodeState.activeCore.participatesInPlanning)
        XCTAssertTrue(NodeState.activeElastic.participatesInPlanning)
        XCTAssertTrue(NodeState.opportunistic.participatesInPlanning)
    }

    // MARK: - apply()

    func testApplyReportsWhetherItMovedAndLeavesStateAloneOnIllegalEvents() {
        var state = NodeState.activeElastic
        XCTAssertTrue(NodeStateMachine.apply(.enteredBackground, to: &state))
        XCTAssertEqual(state, .draining)

        XCTAssertFalse(NodeStateMachine.apply(.returnedToForeground, to: &state))
        XCTAssertEqual(state, .draining, "an illegal event must not silently move the node")
    }

    // MARK: - Initial state

    func testInitialStateFollowsConnectivityAndConsent() {
        XCTAssertEqual(NodeStateMachine.initialState(for: .mac()), .activeCore)
        XCTAssertEqual(NodeStateMachine.initialState(for: .iPhone()), .activeElastic)
        XCTAssertEqual(
            NodeStateMachine.initialState(for: .iPhone(declaresCanHostRequiredStage: false)),
            .opportunistic,
            "a node that withholds consent must never be planned as core"
        )
    }
}
