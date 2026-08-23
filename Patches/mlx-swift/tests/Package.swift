// swift-tools-version: 6.0
//
// A standalone package, deliberately. It is not a target of the root ShareCompute package and not
// an Xcode test target, for two separate reasons:
//
//   - The root `Package.swift` declares zero dependencies, and CLAUDE.md forbids adding one. That
//     boundary is why `ShareComputeCore` builds and tests on Linux at all. Depending on MLX from
//     there would break it.
//   - The Xcode project has no test target -- only an application and a framework -- so adding one
//     means creating a target, build phases and a scheme entry inside `project.pbxproj`, which is
//     generated and which a bad edit leaves unopenable.
//
// SwiftPM resolves the root package's targets only under `Sources/` and `Tests/`, so this package
// is invisible to `swift test` at the repository root. It is built by its own CI job.
import PackageDescription

let package = Package(
    name: "MLXLifecycleChecks",
    platforms: [
        // MLX is Apple-only. This package cannot build on Linux and is not expected to.
        .macOS(.v14)
    ],
    // NOTE: `products` must precede `dependencies`. SwiftPM enforces the argument order of this
    // initializer, and getting it wrong is a *semantic* error that `swiftc -parse` cannot see --
    // the file is perfectly valid Swift either way. That is F18 in miniature, and it broke both
    // macOS jobs on 5047ff6 in 17 seconds.
    products: [
        // Declared so `swift build --product` can name it. The launcher needs a built binary path,
        // not a test bundle, because a ring needs two *processes* -- one per rank -- and XCTest
        // gives one process.
        .executable(name: "RingFormationProbe", targets: ["RingFormationProbe"])
    ],
    dependencies: [
        // The patched fork, at the same branch `project.pbxproj` names. Pinning it here rather than
        // to a commit is intentional: this check should follow the branch the app actually builds
        // against, so a regression pushed to the fork fails here rather than going unnoticed.
        .package(
            url: "https://github.com/joeydd032995-pixel/mlx-swift",
            branch: "sharecompute/free-and-finalize"
        )
    ],
    targets: [
        .executableTarget(
            name: "RingFormationProbe",
            dependencies: [.product(name: "MLX", package: "mlx-swift")],
            path: "RingFormationProbe"
        ),
        .testTarget(
            name: "DistributedGroupLifecycleTests",
            dependencies: [.product(name: "MLX", package: "mlx-swift")],
            // Flattened: without this SwiftPM would want Tests/DistributedGroupLifecycleTests/,
            // giving a redundant `tests/Tests/` path.
            path: "DistributedGroupLifecycleTests"
        )
    ]
)
