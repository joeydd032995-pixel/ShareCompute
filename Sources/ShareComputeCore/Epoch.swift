import Foundation

/// A monotonically increasing generation number for ring membership.
///
/// Membership is versioned rather than mutated because the underlying MLX `DistributedGroup`
/// cannot be joined or left once created (see `findings.md` F1), and every generated token is an
/// all-ranks collective (F2). So a topology change is not an edit to a live group — it is the
/// destruction of one group and the construction of the next. The epoch is what lets every node
/// agree on *which* group a message, lease, or shard plan belongs to, and lets stale messages
/// from a previous generation be discarded rather than acted on.
public struct Epoch: Hashable, Codable, Sendable, Comparable, CustomStringConvertible {
    public let value: UInt64

    public init(_ value: UInt64) {
        self.value = value
    }

    /// The epoch a node occupies before it has ever joined a ring.
    public static let initial = Epoch(0)

    public func next() -> Epoch {
        Epoch(value + 1)
    }

    public var description: String { "epoch \(value)" }

    public static func < (lhs: Epoch, rhs: Epoch) -> Bool {
        lhs.value < rhs.value
    }
}
