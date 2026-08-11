# `mlx` patches

Patches against `ml-explore/mlx`. See [`../README.md`](../README.md) for the repository chain, the
fork list, and the order all the patches must be applied in.

```bash
git clone https://github.com/ml-explore/mlx
cd mlx
git checkout 38ad257088fb2193ad47e527cf6534a689f30943   # the commit mlx-swift pins
git apply /path/to/Patches/mlx/0001-ring-fail-instead-of-hang.patch
git apply /path/to/Patches/mlx/0002-distributed-finalize.patch
```

The pin comes from the `Source/Cmlx/mlx` submodule of `N1k1tung/mlx-swift` at tag
`ios-distrib-0.3.0`, which is what the app builds against.

---

## 0001 — Fail instead of hang

**The bug.** MLX's ring backend fulfils its `std::promise`s only on the success path. After ten
socket errors the worker thread returns, leaving every queued task's promise **unfulfilled and
undestroyed** — so the waiting future never becomes ready, and never receives `broken_promise`
either. One rank leaving wedges every surviving rank permanently, with no exception and no
diagnostic. Sockets are non-blocking with no `SO_RCVTIMEO`, so nothing times out.

This is an upstream defect, not something specific to this project — it affects any MLX ring
cluster — and is worth reporting to `ml-explore/mlx` independently.

**What the patch changes.**

| Change | Why |
|---|---|
| `SocketThread` gains a sticky `failure_` (`std::exception_ptr`); the abort path sets it on every queued promise and clears the queues | Waiters get a real error instead of waiting forever |
| `r == 0` is treated as peer-closed | It used to fall into the `errno` branch, where `errno` is stale from an earlier call and frequently `EAGAIN` — so an *orderly* close (an iOS app being suspended, the common case) never incremented the counter and the loop simply spun |
| `send_impl` / `recv_impl` reject work once failed | The worker has exited; nothing would ever drain a newly queued task |
| All ten `f.wait()` become `f.get()` | **Load-bearing.** `wait()` returns normally on a promise carrying an exception. Setting exceptions without this converts the hang into *silent data corruption*, which is strictly worse |
| The four `encoder.dispatch(...)` lambda bodies are wrapped in `run_guarded` | `CommandEncoder::dispatch` hands the task to `scheduler::enqueue`, and there is no `try`/`catch` anywhere in that path. An escaping exception unwinds a scheduler thread with no handler and calls `std::terminate()`. This also contains three throws that were **already** reachable from inside those lambdas today |
| `GroupImpl::check_healthy()` — virtual, default no-op — overridden by `RingGroup`, called from `to_group()` in `ops.cpp` | The failure is rethrown on the **caller's** thread, where it can legally propagate out through the C API to `mlx_error` and on to mlx-swift's `withError`. Every op in `ops.cpp` funnels through `to_group`, so one call covers all of them |

**No API changes.** Nothing in mlx-c or mlx-swift needs to change for this patch: the error rides
the existing `mlx_error` / `withError` path that `Ring/Manager.swift` already uses.

**Behaviour.** infer-ring runs a collective per token, so a dead peer surfaces as a thrown Swift
error within roughly one token.

**Known gap — the trailing token is emitted.** The generation in flight when the socket dies
completes with garbage before the next collective throws. `ModelManager.streamResponseChunks`
yields each `.text` chunk as it arrives and `ChatViewModel.send` appends every chunk to
`fullReply`, so that garbage token reaches the UI, immediately followed by the ring error.

This is **not** fixed here, and the honest reason is that the obvious fix costs more than the
problem: suppressing it means holding every token back until the next one arrives, adding a token
of latency to every generation to guard against a rare final token. The user sees one junk token
and then a clear error naming the device that left, which is a reasonable trade. If it turns out to
matter, the place to fix it is `ModelManager.streamResponseChunks`.

### Verification

```bash
g++ -std=c++17 -O1 -pthread -o /tmp/stf tests/socket_thread_failure_test.cpp && /tmp/stf
```

MLX is Apple-only and could not be built in the environment this was written in, so verification
was done two ways:

1. **Compilation.** `g++ -fsyntax-only -std=c++17` over the patched `ring.cpp` and `ops.cpp`,
   using the `json` and `fmt` headers vendored in mlx-swift. Both clean.
2. **Runtime semantics.** `tests/socket_thread_failure_test.cpp` mirrors the patched
   `SocketThread` and the guard, and drives them against real `socketpair` sockets. 16 checks, all
   passing, each with a bounded timeout so a regression is *reported* rather than reproduced as a
   hang.

   Both failure routes are covered, because they are genuinely different: a dead peer on the
   **recv** side returns `r == 0` and trips `peer_closed` immediately, whereas on the **send** side
   there is no such signal — `::send` returns `EPIPE` and `error_count` has to climb to
   `max_errors` first. (`SIGPIPE` is ignored in `main`, or writing to the closed peer would kill
   the harness instead of failing a check.)

   The last case mirrors the **unpatched** worker and asserts that it hangs, so the defect is
   pinned rather than merely described.

The test mirrors the patch rather than including it — MLX cannot be compiled here — so it must be
updated in step if `ring.cpp` changes.

**Not verified:** the patch has never been built as part of MLX, nor run on Apple hardware, nor
exercised against a real multi-device ring. Stage 1 of the plan describes the hardware tests.

---

## 0002 — `finalize()`, so the group can be rebuilt

**The bug.** `distributed::init` caches the group it builds in a **function-local static**, keyed by
backend name:

```cpp
Group init(bool strict, const std::string& bk) {
  static std::unordered_map<std::string, std::shared_ptr<detail::GroupImpl>> backends;
  if (auto g = backends.find(bk); g != backends.end()) {
    return Group(g->second);           // <-- every call after the first
  }
```

Nothing can reach that static to clear it. The first `init` wins for the life of the process, so a
second `init` after a node leaves returns the **stale** group and ignores the rewritten hostfile —
the ring "re-forms" with the departed member still in it. This is `findings.md` Spike A, and it is
why milestone 1 shipped detection without re-formation.

The teardown itself was never missing: `~RingGroup()` closes its sockets and `~SocketThread()` sets
`stop_`, notifies and joins its worker. That machinery is correct. It simply never ran, because the
cache held the last reference forever.

**What the patch changes.**

| Change | Why |
|---|---|
| The static moves out of `init` into a file-local `backends()` accessor | A function-local static is unreachable from anywhere else; nothing could clear it |
| New `bool finalize()` drops the cached references | Running the destructors *is* the teardown — no new shutdown path is written |
| `finalize()` checks every impl's use count **before** clearing anything | See below. This is the part worth reviewing |
| `finalize()` returns whether the teardown actually happened | A silent no-op here re-creates the exact bug being fixed |
| A mutex serialises all cache access, in both `init()` and `finalize()` | See below |

**Why the check comes before the clear.** A `Group` handle held anywhere else keeps its impl alive,
so clearing the cache would not destroy it. The first version of this patch cleared first and
reported afterwards via `weak_ptr::expired()` — which is accurate, but leaves the worst possible
state on failure: the old group is gone from the cache yet **still running its socket threads**, and
the next `init()` builds a *second* live ring beside it. Two rings, one process.

Checking first makes failure inert. If any impl is referenced from outside the cache, `finalize()`
returns `false` having changed nothing, and the next `init()` returns what it always would have.
The caller is exactly where they started, which is a state they can reason about.

This was caught by `tests/group_finalize_test.cpp`, not by review — the test asserted the
consequence and the consequence was wrong.

**Why the mutex.** Upstream's `find`/`insert` on the static map were already unsynchronised, but
`finalize()` adds a `clear()`, and `clear()` concurrent with `find()` is undefined behaviour rather
than a lost update. The stronger reason is that the check-then-clear above is only meaningful if it
is *atomic*: without a lock a concurrent `init()` could hand out a `Group` in the window between the
use-count check and the clear, and `finalize()` would then destroy an impl the caller had just taken
a reference to — precisely the case the check exists to prevent.

Two consequences worth knowing:

- **`init()` holds the lock across group construction**, not just the lookup. Locking only around
  `find` and `insert` would let two threads each build a group and each insert it — two live rings in
  one process. The cost is that a slow `ring::init` (waiting on peers) blocks a concurrent
  `finalize()` until it returns.
- **The destructor joins run under the lock.** `cache.clear()` → `~RingGroup()` → `~SocketThread()`
  → `join()`. That is safe only because the ring's socket workers never re-enter this file — they do
  socket I/O and fulfil promises, and touch neither `init()` nor `finalize()`. A worker that could
  reach either would deadlock.

The lock protects the *cache*, not the caller's handles. A `Group` copied or destroyed on another
thread changes a use count without touching the cache, so a concurrent holder can still be missed;
only the quiesce precondition rules that out.

**Why a `bool` rather than `void`.** Ignoring a failed teardown reproduces the original defect
silently: re-init hands back a group whose membership is stale, nothing crashes, and the ring is
merely wrong. That is the same shape as the `wait()`/`get()` defect in 0001, and it gets the same
treatment — the failure is made returnable, and the Swift wrapper deliberately omits
`@discardableResult` so ignoring it has to be written out.

Counting is exact rather than "is the use count 1": `init` stores every group **twice**, once under
`"any"` and once under the resolved backend name, so the cache's own contribution is counted per
impl before comparing.

**Requires** the matching `mlx-c` and `mlx-swift` patches to be reachable from Swift; `mlx-c/0001`
does not compile without this one.

### Verification

```bash
g++ -std=c++17 -O1 -o /tmp/gft tests/group_finalize_test.cpp && /tmp/gft
```

1. **Compilation.** `g++ -fsyntax-only -std=c++17` over the patched `distributed.cpp`, from a
   pristine `38ad2570` checkout with `0001` and `0002` applied in order. Clean.
2. **Coupling.** The mlx-c half was compiled against this patched header, and then against the
   *unpatched* one as a negative control — the latter fails with `'finalize' is not a member of
   'mlx::core::distributed'`. The two patches are genuinely linked, not independently plausible.
3. **Runtime semantics.** `tests/group_finalize_test.cpp` mirrors the cache, `finalize()` and
   `init()`'s cache interaction against a stub impl that records its own destruction. 31 checks, all
   passing, covering: teardown really destroys; two cache keys aliasing one impl destroy it exactly
   once (a double free here is a crash); an outstanding handle blocks teardown and is reported; a
   failed `finalize()` leaves the cache untouched; the supported release → finalize → re-init
   sequence yields a genuinely new group; empty and repeated calls are no-ops.
4. **Thread safety, under ThreadSanitizer.** The harness runs four threads calling `init()` against
   two calling `finalize()` for a bounded 200 ms, asserting that no more than one group is ever alive
   at once. TSan-clean — and deleting the two `lock_guard` lines and re-running reports **43 data
   races**, so the case genuinely exercises what the mutex prevents rather than merely running.

   The last case mirrors the **unpatched** cache and asserts that re-init returns the same group, so
   the defect is pinned rather than merely described.

The test mirrors the patch rather than including it — MLX cannot be compiled here — so it must be
updated in step if `distributed.cpp` changes.

**Not verified:** never built as part of MLX, never run on Apple hardware, never exercised against a
real ring. In particular, nothing here proves that `~RingGroup()` and `~SocketThread()` complete
promptly and without deadlock when the sockets are real and a peer has already gone — the stub impl
has a trivial destructor. That is the first thing to check on hardware.
