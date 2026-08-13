import XCTest
@testable import ShareComputeCore

final class RingWatchdogTests: XCTestCase {

    var clock = TestClock()

    func makeWatchdog(
        stallThreshold: TimeInterval = 10,
        graceAfterLoss: TimeInterval = 2,
        reformationTimeout: TimeInterval = 30,
        maxReformationAttempts: Int = 3
    ) -> RingWatchdog {
        clock = TestClock()
        return RingWatchdog(
            config: RingWatchdogConfig(
                stallThreshold: stallThreshold,
                graceAfterLoss: graceAfterLoss,
                reformationTimeout: reformationTimeout,
                maxReformationAttempts: maxReformationAttempts
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
        XCTAssertFalse(watchdog.isReforming, "a stall alone must not trigger a rebuild")
    }

    func testProgressKeepsTheRingHealthy() {
        let watchdog = makeWatchdog(stallThreshold: 10)
        for _ in 0..<20 {
            clock.advance(5)
            watchdog.recordProgress(at: clock.now)
            XCTAssertEqual(watchdog.evaluate(at: clock.now), .healthy)
        }
    }

    // MARK: - Confirmed loss now starts a rebuild, rather than ending the ring

    func testDepartureFollowedBySilenceStartsReformation() {
        let watchdog = makeWatchdog(graceAfterLoss: 2)
        watchdog.recordLoss(.memberDeparted(NodeID("phone")), at: clock.now)

        clock.advance(1)
        guard case .stalled = watchdog.evaluate(at: clock.now) else {
            return XCTFail("must not confirm loss inside the grace window")
        }

        clock.advance(2)
        guard case let .reforming(reason, _) = watchdog.evaluate(at: clock.now) else {
            return XCTFail("expected .reforming, got \(watchdog.evaluate(at: clock.now))")
        }
        XCTAssertEqual(reason, .memberDeparted(NodeID("phone")))
        XCTAssertTrue(watchdog.isReforming)
        XCTAssertFalse(watchdog.isTerminal, "Stage 2's finalize() makes this recoverable")
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
        watchdog.evaluate(at: clock.now)
        XCTAssertTrue(watchdog.isReforming)
        XCTAssertEqual(watchdog.lossReason, .memberUnreachable(NodeID("phone")))
    }

    /// The first diagnosis names the node that actually broke the ring; later cascading failures
    /// must not overwrite it.
    func testFirstLossReasonIsKept() {
        let watchdog = makeWatchdog(graceAfterLoss: 1)
        watchdog.recordLoss(.memberDeparted(NodeID("phone")), at: clock.now)
        watchdog.recordLoss(.memberUnreachable(NodeID("ipad")), at: clock.now)

        clock.advance(2)
        watchdog.evaluate(at: clock.now)
        XCTAssertEqual(watchdog.lossReason, .memberDeparted(NodeID("phone")))
    }

    // MARK: - Re-formation outcomes, reported by the host

    func testSuccessfulReformationReturnsTheRingToHealthy() {
        let watchdog = makeWatchdog(graceAfterLoss: 1)
        watchdog.recordLoss(.memberDeparted(NodeID("phone")), at: clock.now)
        clock.advance(2)
        watchdog.evaluate(at: clock.now)
        XCTAssertTrue(watchdog.isReforming)

        clock.advance(3)
        watchdog.reformationSucceeded(at: clock.now)

        XCTAssertEqual(watchdog.evaluate(at: clock.now), .healthy)
        XCTAssertFalse(watchdog.isTerminal)
        XCTAssertNil(watchdog.lossReason, "a rebuilt ring carries no loss")
        XCTAssertTrue(
            watchdog.beginGeneration(at: clock.now),
            "the new epoch is a working ring and must admit work"
        )
    }

    /// A failure below the attempt limit leaves the host free to try again.
    func testFailedReformationBelowTheLimitStaysRecoverable() {
        let watchdog = makeWatchdog(graceAfterLoss: 1, maxReformationAttempts: 3)
        watchdog.recordLoss(.memberDeparted(NodeID("phone")), at: clock.now)
        clock.advance(2)
        watchdog.evaluate(at: clock.now)

        watchdog.reformationFailed("finalize() returned false", at: clock.now)
        XCTAssertTrue(watchdog.isReforming)
        XCTAssertFalse(watchdog.isTerminal)
        XCTAssertEqual(watchdog.attemptCount, 1)
    }

    /// Retrying forever is the original hang under another name, so the attempts are bounded — and
    /// the last error is carried out, because the count alone does not say which handle survived.
    func testReformationIsExhaustedAfterTheAttemptLimit() {
        let watchdog = makeWatchdog(graceAfterLoss: 1, maxReformationAttempts: 2)
        watchdog.recordLoss(.memberDeparted(NodeID("phone")), at: clock.now)
        clock.advance(2)
        watchdog.evaluate(at: clock.now)

        watchdog.reformationFailed("finalize() returned false", at: clock.now)
        XCTAssertFalse(watchdog.isTerminal)

        watchdog.reformationFailed("ModelContext still held", at: clock.now)
        XCTAssertTrue(watchdog.isTerminal)
        XCTAssertEqual(
            watchdog.failureCause,
            .reformationExhausted(attempts: 2, lastError: "ModelContext still held")
        )
        XCTAssertEqual(
            watchdog.evaluate(at: clock.now),
            .lost(.memberDeparted(NodeID("phone")),
                  .reformationExhausted(attempts: 2, lastError: "ModelContext still held"))
        )
    }

    /// A host that never reports back must not leave the ring waiting indefinitely — that is the
    /// exact failure this project exists to remove, and a "recovering" label does not excuse it.
    func testReformationThatNeverReportsTimesOut() {
        let watchdog = makeWatchdog(graceAfterLoss: 1, reformationTimeout: 30)
        watchdog.recordLoss(.memberUnreachable(NodeID("phone")), at: clock.now)
        clock.advance(2)
        watchdog.evaluate(at: clock.now)
        XCTAssertTrue(watchdog.isReforming)

        clock.advance(29)
        watchdog.evaluate(at: clock.now)
        XCTAssertTrue(watchdog.isReforming, "still inside the window")

        clock.advance(2)
        XCTAssertEqual(
            watchdog.evaluate(at: clock.now),
            .lost(.memberUnreachable(NodeID("phone")), .reformationTimedOut)
        )
        XCTAssertTrue(watchdog.isTerminal)
    }

    /// Each retry gets its own budget, or a slow second attempt would be killed by the first
    /// attempt's elapsed time.
    func testRetryRestartsTheReformationClock() {
        let watchdog = makeWatchdog(
            graceAfterLoss: 1, reformationTimeout: 30, maxReformationAttempts: 3
        )
        watchdog.recordLoss(.memberDeparted(NodeID("phone")), at: clock.now)
        clock.advance(2)
        watchdog.evaluate(at: clock.now)

        clock.advance(25)
        watchdog.reformationFailed("first attempt failed", at: clock.now)

        clock.advance(20)
        XCTAssertTrue(
            watchdog.isReforming,
            "45s total but only 20s into this attempt — the retry must not inherit the first's clock"
        )
    }

    /// A build without the teardown patch cannot rebuild at all. Going terminal at once is the
    /// honest answer — retrying an absent symbol three times would only delay it.
    func testUnavailableReformationIsTerminalWithoutSpendingAttempts() {
        let watchdog = makeWatchdog(graceAfterLoss: 1, maxReformationAttempts: 3)
        watchdog.recordLoss(.memberDeparted(NodeID("phone")), at: clock.now)
        clock.advance(2)
        watchdog.evaluate(at: clock.now)

        watchdog.reformationUnavailable("built without Patches/mlx-swift/0001", at: clock.now)

        XCTAssertTrue(watchdog.isTerminal)
        XCTAssertEqual(watchdog.attemptCount, 0, "an absent capability is not a failed attempt")
        XCTAssertEqual(
            watchdog.failureCause,
            .reformationFailed("built without Patches/mlx-swift/0001")
        )
        XCTAssertFalse(watchdog.evaluate(at: clock.now).isRecoverable)
    }

    func testProgressDuringReformationDoesNotCancelIt() {
        let watchdog = makeWatchdog(graceAfterLoss: 1)
        watchdog.recordLoss(.memberDeparted(NodeID("phone")), at: clock.now)
        clock.advance(2)
        watchdog.evaluate(at: clock.now)
        XCTAssertTrue(watchdog.isReforming)

        // A late token from the old group must not be read as the ring having healed itself.
        clock.advance(1)
        watchdog.recordProgress(at: clock.now)
        watchdog.evaluate(at: clock.now)
        XCTAssertTrue(watchdog.isReforming)
        XCTAssertEqual(watchdog.lossReason, .memberDeparted(NodeID("phone")))
    }

    func testTerminalLossSurvivesLaterProgress() {
        let watchdog = makeWatchdog(graceAfterLoss: 1, maxReformationAttempts: 1)
        watchdog.recordLoss(.memberDeparted(NodeID("phone")), at: clock.now)
        clock.advance(2)
        watchdog.evaluate(at: clock.now)
        watchdog.reformationFailed("finalize() returned false", at: clock.now)
        XCTAssertTrue(watchdog.isTerminal)

        clock.advance(1)
        watchdog.recordProgress(at: clock.now)
        watchdog.reformationSucceeded(at: clock.now)
        XCTAssertTrue(watchdog.isTerminal, "nothing resurrects a ring that gave up")
    }

    // MARK: - Admission

    func testNewGenerationIsRefusedWhileReforming() {
        let watchdog = makeWatchdog(graceAfterLoss: 1)
        XCTAssertTrue(watchdog.beginGeneration(at: clock.now))

        watchdog.recordLoss(.memberUnreachable(NodeID("phone")), at: clock.now)
        clock.advance(2)

        XCTAssertFalse(
            watchdog.beginGeneration(at: clock.now),
            "issuing work into a group being torn down could only hang"
        )
    }

    func testNewGenerationIsRefusedOnceTheRingIsLost() {
        let watchdog = makeWatchdog(graceAfterLoss: 1, maxReformationAttempts: 1)
        watchdog.recordLoss(.memberUnreachable(NodeID("phone")), at: clock.now)
        clock.advance(2)
        watchdog.evaluate(at: clock.now)
        watchdog.reformationFailed("finalize() returned false", at: clock.now)

        clock.advance(10)
        XCTAssertFalse(watchdog.beginGeneration(at: clock.now))
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
        watchdog.evaluate(at: clock.now)
        XCTAssertEqual(watchdog.lossReason, .memberUnreachable(NodeID("phone")))
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
        XCTAssertFalse(watchdog.isReforming)
    }

    /// End to end against the real membership service: an iPhone announces it is backgrounding,
    /// the ring rebuilds without it, and generation becomes possible again.
    func testBackgroundedPhoneTriggersARebuildRatherThanEndingTheRing() {
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

        clock.advance(3)
        let health = watchdog.evaluate(at: clock.now)
        guard case .reforming = health else {
            return XCTFail("expected .reforming, got \(health)")
        }
        XCTAssertEqual(
            health.userFacingMessage,
            "phone left the ring. Rebuilding the ring without it…"
        )
        XCTAssertTrue(health.isRecoverable)

        // The host tears down and re-initialises at the new epoch.
        clock.advance(4)
        watchdog.reformationSucceeded(at: clock.now)
        XCTAssertEqual(watchdog.evaluate(at: clock.now), .healthy)
        XCTAssertTrue(watchdog.beginGeneration(at: clock.now))
    }

    /// The old message promised a restart was the only option. That is now only true when
    /// re-formation actually failed — and it must still say so then.
    func testTerminalMessageNamesWhyTheRebuildFailed() {
        let watchdog = makeWatchdog(graceAfterLoss: 1, maxReformationAttempts: 1)
        watchdog.recordLoss(.memberDeparted(NodeID("phone")), at: clock.now)
        clock.advance(2)
        watchdog.evaluate(at: clock.now)
        watchdog.reformationFailed("ModelContext still held", at: clock.now)

        let health = watchdog.evaluate(at: clock.now)
        XCTAssertFalse(health.isRecoverable)
        XCTAssertEqual(
            health.userFacingMessage,
            "phone left the ring — rebuilding the ring failed 1 times — ModelContext still held. "
                + "Restart the app to try again."
        )
    }

    func testHealthyRingHasNoUserFacingMessage() {
        XCTAssertNil(RingHealth.healthy.userFacingMessage)
        XCTAssertNotNil(RingHealth.stalled(since: clock.now).userFacingMessage)
    }
}
