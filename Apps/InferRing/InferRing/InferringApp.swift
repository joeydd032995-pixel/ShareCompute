//
import SwiftUI
import Ring

@main
struct InferringApp: App {
    @State private var bonjourClient = BonjourClient()
    @State private var bonjourServer = BonjourServer()
    @State private var ringCoordinator = RingCoordinator()
    @State private var modelManager = ModelManager()
    @State private var ringHealthMonitor = RingHealthMonitor()
    private let dataServer = DataServer()

    init() {
        DI.register(bonjourClient)
        DI.register(ringCoordinator)
        DI.register(modelManager)
        DI.register(ringHealthMonitor)
        dataServer.start()
        ringCoordinator.start()

        Task.detached(priority: .background) {
            await ModelCards.checkLoadedModels()
        }
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
        }
        .environment(bonjourClient)
        .environment(bonjourServer)
        .environment(ringCoordinator)
        .environment(modelManager)
        .environment(ringHealthMonitor)
    }
}
