# MLX patches

Patches against MLX that infer-ring needs in order to survive a node leaving the ring.

They are kept here as patch files rather than as forks because forking
`ml-explore/mlx`, `ml-explore/mlx-c` and `N1k1tung/mlx-swift` creates repositories under an
account, which is a decision for the repository owner rather than something to do implicitly.

## Applying

```bash
git clone https://github.com/ml-explore/mlx
cd mlx
git checkout 38ad257088fb2193ad47e527cf6534a689f30943   # the commit mlx-swift pins
git apply /path/to/Patches/mlx/0001-ring-fail-instead-of-hang.patch
```

The pin comes from the `Source/Cmlx/mlx` submodule of
`N1k1tung/mlx-swift` at branch `ios-distrib-0.3.0`, which is what the app builds against.
`Cmlx` is a **source** SwiftPM target, not a prebuilt binary, so a patched submodule is compiled
directly — there is no CMake step and no artifact to regenerate.

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
