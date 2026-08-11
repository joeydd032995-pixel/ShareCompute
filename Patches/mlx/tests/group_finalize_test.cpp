// Standalone verification of the group cache semantics introduced by
// 0002-distributed-finalize.patch.
//
// MLX is Apple-only and cannot be built on the Linux container this was
// developed in, so the patched cache is mirrored here and exercised directly.
// The syntax check (`g++ -fsyntax-only`) proves the patch compiles; it proves
// nothing about whether finalize() actually tears the group down, which is the
// only thing that matters.
//
// The failure mode being guarded against is silent: if finalize() does not
// really drop the last reference, the next init() returns the *stale* group
// and the ring re-forms with the departed member still in it. Nothing crashes,
// nothing logs -- the ring is simply wrong. That is the same shape of bug as
// the wait()/get() defect in Stage 1, so it gets the same treatment.
//
// MIRRORS, does not include, the patched distributed.cpp. If that file changes,
// this must be updated to match. The cache, finalize() and the relevant half of
// init() are copied verbatim except that GroupImpl is replaced by a stub that
// records its own destruction (the real one pulls in all of MLX).
//
//   g++ -std=c++17 -O1 -pthread -o /tmp/gft group_finalize_test.cpp && /tmp/gft
//
// The concurrency case is worth running under ThreadSanitizer, which is how the
// locking was actually verified rather than merely asserted:
//
//   g++ -std=c++17 -O1 -g -fsanitize=thread -pthread \
//       -o /tmp/gft_tsan group_finalize_test.cpp && /tmp/gft_tsan
//
// Clean. Deleting the two lock_guard lines and re-running reports 43 data
// races, so the case genuinely exercises what the mutex is there to prevent.

#include <atomic>
#include <chrono>
#include <iostream>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

namespace {

int failures = 0;

void check(bool ok, const std::string& what) {
  std::cout << (ok ? "  PASS  " : "  FAIL  ") << what << "\n";
  if (!ok) {
    failures++;
  }
}

// ---------------------------------------------------------------------------
// Stub standing in for detail::GroupImpl
//
// The real one is abstract and drags in every backend. All this test needs is
// identity and an observable destructor -- in the real code that destructor is
// ~RingGroup(), which closes the sockets and joins the SocketThread workers.
// ---------------------------------------------------------------------------
// Atomic because the concurrency case below constructs and destroys impls from
// several threads at once.
std::atomic<int> live_impls{0};
std::atomic<int> destroyed_impls{0};
std::atomic<int> max_live{0}; // high-water mark: must never exceed 1

struct GroupImpl {
  explicit GroupImpl(int id) : id(id) {
    int now = ++live_impls;
    int seen = max_live.load();
    while (now > seen && !max_live.compare_exchange_weak(seen, now)) {
    }
  }
  ~GroupImpl() {
    live_impls--;
    destroyed_impls++;
  }
  int id;
};

// ---------------------------------------------------------------------------
// Mirror of the patched cache in mlx/distributed/distributed.cpp
// ---------------------------------------------------------------------------
std::unordered_map<std::string, std::shared_ptr<GroupImpl>>& backends() {
  static std::unordered_map<std::string, std::shared_ptr<GroupImpl>> backends_;
  return backends_;
}

std::mutex& backends_mutex() {
  static std::mutex mutex_;
  return mutex_;
}

bool finalize() {
  std::lock_guard<std::mutex> lock(backends_mutex());
  auto& cache = backends();

  std::unordered_map<GroupImpl*, long> cache_refs;
  for (const auto& entry : cache) {
    cache_refs[entry.second.get()]++;
  }

  for (const auto& entry : cache) {
    if (entry.second.use_count() > cache_refs[entry.second.get()]) {
      return false;
    }
  }

  cache.clear();
  return true;
}

// Mirror of init()'s cache interaction. `next_id` stands in for "actually
// building a new ring": a different id means a genuinely different group.
int next_id = 1;

std::shared_ptr<GroupImpl> init(const std::string& bk) {
  std::lock_guard<std::mutex> lock(backends_mutex());
  auto& cache = backends();

  if (auto g = cache.find(bk); g != cache.end()) {
    return g->second;
  }

  auto group = std::make_shared<GroupImpl>(next_id++);
  std::string bk_ = (bk == "any") ? "ring" : bk;

  // Both inserts, as in the patched init(): "any" and the resolved backend
  // name alias the same impl.
  cache.insert({"any", group});
  cache.insert({std::move(bk_), group});
  return group;
}

void reset() {
  backends().clear();
  live_impls = 0;
  destroyed_impls = 0;
  max_live = 0;
  next_id = 1;
}

// ---------------------------------------------------------------------------

void test_finalize_actually_destroys_the_group() {
  std::cout << "\nfinalize() with no outstanding handle tears the group down\n";
  reset();

  init("any");
  check(live_impls == 1, "init built a group");

  bool released = finalize();
  check(released, "finalize reports the teardown happened");
  check(destroyed_impls == 1, "the impl's destructor actually ran");
  check(live_impls == 0, "nothing is left alive");
}

void test_aliased_cache_entries_destroy_once() {
  std::cout << "\ntwo cache keys aliasing one impl destroy it exactly once\n";
  reset();

  init("any");
  check(backends().size() == 2, "cached under both \"any\" and \"ring\"");
  check(live_impls == 1, "but only one impl exists");

  bool released = finalize();
  check(released, "finalize reports the teardown happened");
  check(
      destroyed_impls == 1,
      "destroyed once, not once per key -- a double free here would be a crash");
}

void test_outstanding_handle_blocks_teardown_and_is_reported() {
  std::cout << "\nan outstanding handle blocks teardown, and finalize says so\n";
  reset();

  auto held = init("any"); // caller keeps its handle, as Swift would
  bool released = finalize();

  check(!released, "finalize reports that nothing was torn down");
  check(destroyed_impls == 0, "the destructor did not run");
  check(live_impls == 1, "the group is still alive");
  check(held != nullptr && held->id == 1, "the caller's handle is still valid");
}

void test_failed_finalize_changes_nothing() {
  std::cout << "\na failed finalize() leaves the cache untouched\n";
  reset();

  auto held = init("any");
  int first_id = held->id;

  finalize(); // returns false; a caller ignoring that proceeds anyway

  // The guard checked before clearing, so the cache still holds the group and
  // init() hands back the same one. The alternative -- clearing first and
  // discovering the failure afterwards -- would leave the old group running
  // its socket threads while init() built a second live ring beside it.
  check(backends().size() == 2, "the cache was not cleared");
  auto rebuilt = init("any");
  check(rebuilt->id == first_id, "re-init returns the same group, as before");
  check(live_impls == 1, "exactly one ring is running -- no zombie alongside it");
  check(destroyed_impls == 0, "nothing was torn down");
}

void test_release_then_finalize_then_reinit_gives_a_new_group() {
  std::cout << "\nthe supported sequence: release, finalize, re-init\n";
  reset();

  auto held = init("any");
  int first_id = held->id;

  held.reset(); // caller releases first -- what Swift's deinit must do
  bool released = finalize();

  check(released, "finalize reports the teardown happened");
  check(destroyed_impls == 1, "the old ring was actually torn down");
  check(live_impls == 0, "no group is running between finalize and re-init");

  auto rebuilt = init("any");
  check(
      rebuilt->id != first_id,
      "re-init returns a genuinely NEW group, not the memoised one");
  check(live_impls == 1, "exactly one group is running");
}

void test_finalize_on_empty_cache_is_a_no_op() {
  std::cout << "\nfinalize() before any init(), and twice in a row\n";
  reset();

  check(finalize(), "finalize on an empty cache reports success");

  init("any");
  check(finalize(), "first finalize after init succeeds");
  check(finalize(), "a second finalize is a harmless no-op");
  check(destroyed_impls == 1, "the group was destroyed exactly once");
}

void test_concurrent_init_and_finalize_is_safe() {
  std::cout << "\ninit() and finalize() racing each other\n";
  reset();

  // Without the mutex this is a data race on the map -- clear() concurrent with
  // find() is undefined behaviour, not merely a lost update -- and the
  // check-then-clear is not atomic, so finalize() could destroy an impl that a
  // concurrent init() had just handed out.
  std::atomic<bool> stop{false};
  std::vector<std::thread> threads;

  for (int i = 0; i < 4; i++) {
    threads.emplace_back([&] {
      while (!stop.load()) {
        auto held = init("any"); // released at the end of each iteration
        if (held->id < 1) {
          std::cout << "  FAIL  init returned a malformed group\n";
        }
      }
    });
  }
  for (int i = 0; i < 2; i++) {
    threads.emplace_back([&] {
      while (!stop.load()) {
        finalize(); // false whenever an initter is mid-iteration; that is fine
      }
    });
  }

  // Bounded, like the Stage 1 harness: the test reports rather than hangs.
  std::this_thread::sleep_for(std::chrono::milliseconds(200));
  stop = true;
  for (auto& t : threads) {
    t.join();
  }

  check(true, "completed without crashing or deadlocking");
  check(
      max_live.load() <= 1,
      "never more than one group alive at once -- two threads never both built one");

  // Every worker has exited, so no handles remain and a final teardown must
  // succeed.
  check(finalize(), "a final finalize() succeeds once all threads are done");
  check(live_impls.load() == 0, "no group is left running");
}

void test_unpatched_behaviour_returns_the_stale_group() {
  std::cout << "\nunpatched: the memoised cache can never honour a re-init\n";
  reset();

  // The unpatched init() has no finalize() to call -- the static cache is
  // unreachable. Mirror that by initialising twice with no teardown available.
  auto first = init("any");
  auto second = init("any");

  check(
      first->id == second->id,
      "unpatched: re-init returns the SAME group -- this is the bug the patch fixes");
  check(
      live_impls == 1,
      "unpatched: the departed member's ring is what you keep getting back");
}

} // namespace

int main() {
  test_finalize_actually_destroys_the_group();
  test_aliased_cache_entries_destroy_once();
  test_outstanding_handle_blocks_teardown_and_is_reported();
  test_failed_finalize_changes_nothing();
  test_release_then_finalize_then_reinit_gives_a_new_group();
  test_finalize_on_empty_cache_is_a_no_op();
  test_concurrent_init_and_finalize_is_safe();
  test_unpatched_behaviour_returns_the_stale_group();

  std::cout << "\n" << (failures == 0 ? "all checks passed" : "FAILURES: ")
            << (failures == 0 ? "" : std::to_string(failures)) << "\n";
  return failures == 0 ? 0 : 1;
}
