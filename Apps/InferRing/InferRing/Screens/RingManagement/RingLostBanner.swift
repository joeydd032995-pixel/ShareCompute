import SwiftUI
import ShareComputeCore

/// Shown when a device has left the ring and the ring cannot continue.
///
/// The wording is deliberately blunt about needing a restart. The MLX group cannot be rebuilt in
/// this process — `distributed::init` returns its cached group on every call after the first — so
/// offering a "Reconnect" button would promise a recovery that cannot happen. Naming the device
/// that left is the most useful thing we can tell the user.
struct RingLostBanner: View {
    let reason: RingLossReason

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

                Text("\(reason.description). Restart the app to form a new ring.")
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

#Preview {
    RingLostBanner(reason: .memberDeparted(NodeID("InferRing-iPhone")))
        .padding()
        .background(Color.black)
}
