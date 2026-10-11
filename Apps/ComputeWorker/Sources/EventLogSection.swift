import SwiftUI
import UniformTypeIdentifiers

/// The log as a file for the share sheet. The file is built when the operator picks a destination,
/// not when this screen appears, so it holds everything up to that moment. It reads the log on its
/// own queue and does not touch `WorkerModel`, so sharing cannot disturb a running session.
struct EventLogExport: Transferable, Sendable {
    static var transferRepresentation: some TransferRepresentation {
        FileRepresentation(exportedContentType: .plainText) { _ in
            let url = try EventLog.shared.makeExportFile()
            return SentTransferredFile(url)
        }
    }
}

/// Always enabled: it works while connected, while idle, and after a failure.
struct EventLogSection: View {
    var body: some View {
        Section("Event log") {
            ShareLink(item: EventLogExport(), preview: SharePreview("ShareCompute iPhone event log")) {
                Label("Share event log", systemImage: "square.and.arrow.up")
            }
            Text("Sends a text file of what this iPhone did: connections, disconnects, memory and heat warnings. It never contains your pairing code. Send it after any failed test, even if you reopened the app first.")
            Text("The same file is in the Files app under On My iPhone › ShareCompute Worker › logs.")
        }
    }
}
