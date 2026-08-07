// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "ShareCompute",
    platforms: [
        .macOS(.v14),
        .iOS(.v17),
    ],
    products: [
        .library(name: "ShareComputeCore", targets: ["ShareComputeCore"]),
    ],
    targets: [
        // Platform-neutral core. Deliberately has NO dependencies: not MLX, not UIKit,
        // not even SwiftNIO. Transport is expressed as a protocol (RingTransport) that
        // the host application implements. This keeps the core buildable and testable
        // on Linux and Windows, which is what makes the spec's later desktop adapters
        // possible without an FFI layer.
        .target(
            name: "ShareComputeCore",
            swiftSettings: [.swiftLanguageMode(.v6)]
        ),
        .testTarget(
            name: "ShareComputeCoreTests",
            dependencies: ["ShareComputeCore"],
            swiftSettings: [.swiftLanguageMode(.v6)]
        ),
    ]
)
