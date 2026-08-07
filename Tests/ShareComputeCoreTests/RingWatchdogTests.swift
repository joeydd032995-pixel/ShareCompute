import XCTest
@testable import ShareComputeCore

final class RingWatchdogTests: XCTestCase {

    var clock = TestClock()

    func makeWatchdog(
        stallThreshold: TimeInterval = 10,
        graceAfterLoss: TimeInterval = 2
    ) -> RingWatchdog {
        clock = TestClock()
        return RingWatchdog(
            config: RingWatchdogConfig(
                stallThreshold: stallThreshold, graceAfterLoss: graceAfterLoss
            ),
            startedAt: clock.now
        )
    }

    // MARK: - The false-positive this design exists to avoid

    /// A large prefill on a phone legitimately produces nothing for a long time. Slow must never
    /// be mistaken for dead, or the watchdog would abort healthy work.
    func testLongSilenceWithIntactMembershipIsNeverDeclaredLost() {
        let watchdog = makeWatchdog(stallThreshold: 10)

        clock.advance(9)
        XCTAssertEqual(watchdog.evaluate(at: clock.now), .healthy)

        clock.advance(500)
        guard case .stalled = watchdog.evaluate(at: clock.now) else {
            return XCTFail("expected .stalled, got \(watchdog.evaluate(at: clock.now))")
        }
        XCTAssertFalse(watchdog.isTerminal, "a stall alone must never be terminal")
    }

    func testProgressKeepsTheRingHealthy() {
        let watchdog = makeWatchdog(stallThreshold: 10)
        for _ in 0..<20 {
            clock.advance(5)
            watchdog.recordProgress(at: clock.now)
            XCTAssertEqual(watchdog.evaluate(at: clock.now), .healthy)
        }
    }

    // MARK: - Loss

    func testDepartureFollowedBySilenceIsDeclaredLost() {
        let watchdog = makeWatchdog(graceAfterLoss: 2)
        watchdog.recordLoss(.memberDeparted(NodeID("phone")), at: clock.now)

        clock.advance(1)
        guard case .stalled = watchdog.evaluate(at: clock.now) else {
            return XCTFail("must not declare loss inside the grace window")
        }

        clock.advance(2)
        XCTAssertEqual(
            watchdog.evaluate(at: clock.now), .lost(.memberDeparted(NodeID("phone")))
        )
        XCTAssertTrue(watchdog.isTerminal)
    }

    /// Tokens already in flight when the departure notice arrives must be allowed to land.
    func testTokensStillArrivingPushTheDeadlineOut() {
        let watchdog = makeWatchdog(graceAfterLoss: 2)
        watchdog.recordLoss(.memberUnreachable(NodeID("phone")), at: clock.now)

        for _ in 0..<5 {
            clock.advance(1)
            watchdog.recordProgress(at: clock.now)
            guard case .stalled = watchdog.evaluate(at: clock.now) else {
                return XCTFail("a generation still producing tokens must not be cut short")
            }
        }

        clock.advance(3)
        XCTAssertEqual(
            watchdog.evaluate(at: clock.now), .lost(.memberUnreachable(NodeID("phone")))
        )
    }

    /// The first diagnosis names the node that actually broke the ring; later cascading failures
    /// must not overwrite it.
    func testFirstLossReasonIsKept() {
        let watchdog = makeWatchdog(graceAfterLoss: 1)
        watchdog.recordLoss(.memberDeparted(NodeID("phone")), at: clock.now)
        watchdog.recordLoss(.memberUnreachable(NodeID("ipad")), at: clock.now)

        clock.advance(2)
        XCTAssertEqual(watchdog.evaluate(at: clock.now), .lost(.memberDeparted(NodeID("phone"))))
    }

    // MARK: - Terminal behaviour

    /// The MLX group cannot be rebuilt in-process, so a lost ring stays lost. Anything else would
    /// promise a recovery that cannot happen.
    func testLossIsTerminalAndSurvivesLaterProgress() {
        let watchdog = makeWatchdog(graceAfterLoss: 1)
        watchdog.recordLoss(.memberDeparted(NodeID("phone")), at: clock.now)
        clock.advance(2)
        // Confirmation happens on evaluation; `isTerminal` reports confirmed loss only.
        XCTAssertEqual(watchdog.evaluate(at: clock.now), .lost(.memberDeparted(NodeID("phone"))))
        XCTAssertTrue(watchdog.isTerminal)

        clock.advance(1)
        watchdog.recordProgress(at: clock.now)
        XCTAssertEqual(watchdog.evaluate(at: clock.now), .lost(.memberDeparted(NodeID("phone"))))
    }

    func testNewGenerationIsRefusedOnceTheRingIsLost() {
        let watchdog = makeWatchdog(graceAfterLoss: 1)
        XCTAssertTrue(watchdog.beginGeneration(at: clock.now))

        watchdog.recordLoss(.memberUnreachable(NodeID("phone")), at: clock.now)
        clock.advance(2)
        watchdog.evaluate(at: clock.now)

        clock.advance(10)
        XCTAssertFalse(
            watchdog.beginGeneration(at: clock.now),
            "issuing new work into a dead group could only hang"
        )
    }

    func testBeginGenerationClearsAStaleUnconfirmedLoss() {
        let watchdog = makeWatchdog(graceAfterLoss: 5)
        watchdog.recordLoss(.memberDeparted(NodeID("phone")), at: clock.now)

        // Loss noted but never confirmed — the generation finished first.
        clock.advance(1)
        XCTAssertTrue(watchdog.beginGeneration(at: clock.now))

        clock.advance(4)
        XCTAssertEqual(watchdog.evaluate(at: clock.now), .healthy)
    }

    // MARK: - Wiring to membership

    func testMembershipEventsAreTranslatedToLossReasons() {
        let watchdog = makeWatchdog(graceAfterLoss: 1)
        watchdog.consume(
            [
                .nodeJoined(NodeID("ipad")),
                .nodeEvicted(NodeID("phone"), reason: .heartbeatLost),
            ],
            at: clock.now
        )
        clock.advance(2)
        XCTAssertEqual(watchdog.evaluate(at: clock.now), .lost(.memberUnreachable(NodeID("phone"))))
    }

    func testJoinsAndEpochChangesDoNotTriggerLoss() {
        let watchdog = makeWatchdog(graceAfterLoss: 1)
        watchdog.consume(
            [
                .nodeJoined(NodeID("ipad")),
                .epochChanged(Epoch(2), members: [NodeID("mac"), NodeID("ipad")]),
                .epochChangeDeferred(untilDwellElapsed: 3),
            ],
            at: clock.now
        )
        clock.advance(30)
        XCTAssertFalse(watchdog.isTerminal)
    }

    /// End to end against the real membership service: an iPhone announces it is backgrounding,
    /// and the generation is reported as failed instead of hanging.
    func testBackgroundedPhoneEndsTheGenerationWithANamedReason() {
        let clock = TestClock()
        let membership = MembershipService(localNodeID: NodeID("mac"), clock: clock)
        let watchdog = RingWatchdog(
            config: RingWatchdogConfig(stallThreshold: 10, graceAfterLoss: 2),
            startedAt: clock.now
        )

        membership.nodeAppeared(profile: .mac("mac"))
        membership.nodeAppeared(profile: .iPhone("phone"))
        clock.advance(10)
        watchdog.consume(membership.tick(at: clock.now), at: clock.now)

        // Tokens are flowing.
        watchdog.recordProgress(at: clock.now)
        XCTAssertEqual(watchdog.evaluate(at: clock.now), .healthy)

        // Phone goes to the background mid-generation.
        watchdog.consume(membership.nodeAnnouncedDrain(NodeID("phone")), at: clock.now)

        // MLX is now wedged: no further tokens will ever arrive.
        clock.advance(3)
        let health = watchdog.evaluate(at: clock.now)

        XCTAssertEqual(health, .lost(.memberDeparted(NodeID("phone"))))
        let message = try? XCTUnwrap(health.userFacingMessage)
        XCTAssertEqual(
            message,
            "phone left the ring. This ring can't be rebuilt without restarting the app."
        )
    }

    func testHealthyRingHasNoUserFacingMessage() {
        XCTAssertNil(RingHealth.healthy.userFacingMessage)
        XCTAssertNotNil(RingHealth.stalled(since: clock.now).userFacingMessage)
    }
}
