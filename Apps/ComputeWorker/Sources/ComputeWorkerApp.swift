import SwiftUI
import UniformTypeIdentifiers

@main
struct ComputeWorkerApp: App {
    @StateObject private var model = WorkerModel()
    @Environment(\.scenePhase) private var phase
    @State private var importing = false
    @State private var scanning = false
    var body: some Scene {
        WindowGroup {
            NavigationStack {
                Form {
                    Section("Pair with your laptop") {
                        Button("Scan laptop QR") { model.stop("Scanning…"); scanning = true }
                        DisclosureGroup("Paste code or import file") {
                            TextEditor(text: $model.pairingText).frame(minHeight: 110)
                                .autocorrectionDisabled().textInputAutocapitalization(.never)
                            Button("Import iphone.json") { importing = true }
                        }
                        Button(model.running ? "Disconnect" : "Connect") {
                            if model.running { model.stop("Disconnected") } else { model.start() }
                        }
                    }
                    Section("Native compute worker") {
                        Text(model.status)
                        LabeledContent("Allocated", value: "\(model.allocated / 1_048_576) MiB")
                        LabeledContent("Peak buffers", value: "\(model.peak / 1_048_576) MiB")
                        LabeledContent("Completed graphs", value: "\(model.graphs)")
                        Text("Keep this app open while computing. Leaving the app disconnects the worker and fails the active generation.")
                    }
                }.navigationTitle("ShareCompute")
            }
            .sheet(isPresented: $scanning) {
                NavigationStack {
                    QRScanner { text in model.pairingText = text; scanning = false; model.start() }
                        .navigationTitle("Scan iPhone QR")
                        .toolbar { Button("Cancel") { scanning = false } }
                }
            }
            .fileImporter(isPresented: $importing, allowedContentTypes: [.json]) { result in
                do {
                    let url = try result.get(); let scoped = url.startAccessingSecurityScopedResource()
                    defer { if scoped { url.stopAccessingSecurityScopedResource() } }
                    model.pairingText = try String(contentsOf: url, encoding: .utf8)
                } catch { model.status = error.localizedDescription }
            }
            .onAppear { model.startSimulatorIfRequested() }
            .onChange(of: phase) { if $0 == .background { model.stop("Backgrounded — reconnect before the next run") } }
        }
    }
}
