//
import Foundation
import Darwin
import MLX
import MLXLMCommon
import MLXLLM
import MLXVLM
import MLXNN

public final class MLXManager {
    public init() {}

    private var group: DistributedGroup?

    public func initMLX(rank: Int, devices: [String]) throws {
        let port = 13373
        let json = try JSONEncoder().encode(devices.map {
            ["\($0):\(port)", "\($0):\(port+1)"]
        })
        let cachesDir = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first!
        if !FileManager.default.fileExists(atPath: cachesDir.path) {
            try FileManager.default.createDirectory(at: cachesDir, withIntermediateDirectories: true)
        }
        let hostfileUrl = cachesDir.appendingPathComponent("mlx_hostfile.json")
        try json.write(to: hostfileUrl)
        print("Initializing MLX ring with rank \(rank)")
        setenv("MLX_HOSTFILE", hostfileUrl.path, 1)
        setenv("MLX_RANK", "\(rank)", 1)
        #if DEBUG
        setenv("MLX_RING_VERBOSE", "1", 1)
        #endif

        try MLX.withError {
            group = DistributedGroup.initialize(strict: true)
        }
    }

    /// Tears the distributed group down so a new epoch can be formed.
    ///
    /// **The caller must have released the loaded `ModelContext` first.** This is not a style
    /// preference: `ModelContext → model → sharded layers → DistributedGroup` (`findings.md` F14),
    /// so while a sharded model is loaded the layers still hold the group, `finalize()` returns
    /// `false`, and by design nothing is torn down. `MLXManager` cannot enforce this because it does
    /// not own the context — `ModelManager` does.
    ///
    /// Throws `RingError.handlesOutlivedTeardown` on a `false`. Treating that as success would be
    /// the worst available outcome: the next `initMLX` returns the *same* cached group, so the ring
    /// silently re-forms with the departed member still in it. Stage 2's check-before-clear is what
    /// makes this visible rather than silent — see `Patches/mlx/0002`.
    ///
    /// Not verified: no part of this has run. `finalize()` has never been executed on hardware, and
    /// whether releasing `ModelContext` actually drops every layer reference is F14's open question.
    public func teardown() throws {
        // Drop our own reference first. `finalize()` inspects use counts, so a handle still held
        // here would defeat it just as surely as one held by the model.
        group = nil

        guard DistributedGroup.finalize() else {
            throw RingError.handlesOutlivedTeardown
        }
    }

    /// Tear down and re-initialise at a new epoch, in the one order that works.
    ///
    /// Exists so the sequence cannot be got wrong by assembling it at the call site: re-initialising
    /// after a failed teardown is the specific mistake that produces a stale ring with no error, and
    /// `teardown()` throwing before `initMLX` is ever reached is what prevents it.
    public func reform(rank: Int, devices: [String]) throws {
        try teardown()
        try initMLX(rank: rank, devices: devices)
    }

    /// Whether a group is currently held. Used to tell "never initialised" from "torn down".
    public var hasGroup: Bool { group != nil }

    public func synchronize() {
        guard let group else {
            print("group not initialized")
            return
        }

        group.allSum(MLXArray(1.0)).eval()
    }

    public func validate() {
        guard let group else {
            print("group not initialized")
            return
        }
        let key = MLXRandom.key(0)
        let value = MLXRandom.uniform(-100.0 ..< 100, [2,4,6], key: key)
        let f16 = value.asType(.float16)
        let sum = group.allSum(f16)
        let size = Float(group.size)
        let expected = f16 * size
        let diff = abs(sum - expected).max()
        
        if diff.item(Float.self) < 1e-3 {
            print("Distributed validation passed!")
        } else {
            print("Distributed validation failed! Max difference: \(diff.item(Float.self))")
        }
    }

    public func loadModel(
        _ card: ModelCard,
        shardMeta: ShardMetadata,
        progressHandler: @Sendable @escaping (Progress) -> Void
    ) async throws -> ModelContext {
        Memory.clearCache()

        let factory: ModelFactory = card.isVisionModel ? VLMModelFactory.shared : LLMModelFactory.shared
        var context = try await factory.load(
            configuration: ModelConfiguration(id: card.modelId),
            lazy: group != nil,
            progressHandler: progressHandler
        )

        if let group {
            if shardMeta.useTensorParallel && card.metadata.supportsTensor {
                context.model = tensorAutoParallel(
                    model: context.model,
                    group: group
                )
            }
            else {
                context.model = pipelineAutoParallel(
                    model: context.model,
                    group: group,
                    modelShardMeta: shardMeta
                )
            }
            eval(context.model)
        }

        return context
    }

}
