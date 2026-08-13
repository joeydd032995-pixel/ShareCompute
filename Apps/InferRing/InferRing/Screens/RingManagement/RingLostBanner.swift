import SwiftUI
import ShareComputeCore

/// Shown while the ring is being rebuilt at a new epoch after a device left.
///
/// Deliberately not styled as an error. Since Stage 3 this is a recoverable state — the group is
/// torn down and re-initialised without the departed member — so presenting it in the same alarming
/// orange as a terminal loss would tell the user something untrue.
struct RingReformingBanner: View {
    let reason: RingLossReason

    var body: some View {
        HStack {
            ProgressView()
                .progressViewStyle(.circular)
                .tint(.white)

            VStack(alignment: .leading, spacing: 4) {
                Text("Rebuilding Ring")
                    .font(.callout)
                    .bold()
                    .foregroundStyle(.white)

                Text("\(reason.description). Reconnecting the remaining devices…")
                    .font(.caption)
                    .foregroundStyle(.white.opacity(0.9))
            }
            .fixedSize(horizontal: false, vertical: true)

            Spacer(minLength: 16)
        }
        .padding()
        .background(Color.blue.opacity(0.9))
        .cornerRadius(12)
    }
}

/// Shown when a device has left and the ring could not be rebuilt.
///
/// The wording is blunt about needing a restart, and that is now conditional rather than universal.
/// Before Stage 3 it was always true: `distributed::init` returned its cached group forever, so no
/// in-process recovery existed. `Patches/mlx/0002` changed that, so a restart is only the answer
/// once re-formation has actually failed — and `cause` says how, because "it broke" without "why"
/// leaves the user nothing to act on.
struct RingLostBanner: View {
    let reason: RingLossReason
    let cause: RingFailureCause?

    private var detail: String {
        guard let cause else {
            return "\(reason.description). Restart the app to form a new ring."
        }
        return "\(reason.description) — \(cause.description). Restart the app to try again."
    }

    var body: some View {
        HStack {
            Image(systemName: "link.badge.plus")
                .symbolVariant(.slash)
                .foregroundStyle(.white)
                .font(.title2)

            VStack(alignment: .leading, spacing: 4) {
                Text("Ring Disconnected")
                    .font(.callout)
                    .bold()
                    .foregroundStyle(.white)

                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.white.opacity(0.9))
            }
            .fixedSize(horizontal: false, vertical: true)

            Spacer(minLength: 16)
        }
        .padding()
        .background(Color.orange.opacity(0.9))
        .cornerRadius(12)
    }
}

#Preview("Reforming") {
    RingReformingBanner(reason: .memberDeparted(NodeID("InferRing-iPhone")))
        .padding()
        .background(Color.black)
}

#Preview("Lost") {
    RingLostBanner(
        reason: .memberDeparted(NodeID("InferRing-iPhone")),
        cause: .reformationExhausted(attempts: 3, lastError: "a group handle was still held")
    )
    .padding()
    .background(Color.black)
}
