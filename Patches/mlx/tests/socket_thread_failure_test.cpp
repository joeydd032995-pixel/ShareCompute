// Standalone verification of the failure semantics introduced by
// 0001-ring-fail-instead-of-hang.patch.
//
// MLX is Apple-only and cannot be built on the Linux container this was
// developed in, so the patched `SocketThread` logic is mirrored here against
// real sockets (socketpair) and exercised directly. This is the part of the
// patch most likely to be wrong -- promises, a worker thread, and a socket
// dying underneath it -- and the part whose failure mode (an indefinite hang)
// is hardest to notice in an integration test.
//
// MIRRORS, does not include, the patched ring.cpp. If that file changes, this
// must be updated to match. Everything below is copied verbatim from the patch
// except that mlx's `log_info` is stubbed out.
//
//   g++ -std=c++17 -O1 -pthread -o /tmp/stf socket_thread_failure_test.cpp && /tmp/stf
//
// Every wait uses a bounded timeout: if the patch regresses to the old
// behaviour the test reports a hang rather than hanging itself.

#include <fcntl.h>
#include <sys/socket.h>
#include <unistd.h>

#include <chrono>
#include <condition_variable>
#include <cstring>
#include <future>
#include <iostream>
#include <list>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>

namespace {

template <typename... Args>
void log_info(bool, Args&&...) {} // stubbed; mlx logs here

// ---------------------------------------------------------------------------
// Mirror of the patched SocketThread
// ---------------------------------------------------------------------------
class SocketThread {
 public:
  SocketThread(int fd) : fd_(fd), stop_(false) {
    worker_ = std::thread(&SocketThread::worker, this);
    int flags = fcntl(fd, F_GETFL, 0);
    fcntl(fd, F_SETFL, flags | O_NONBLOCK);
  }
  ~SocketThread() {
    stop_ = true;
    condition_.notify_all();
    worker_.join();
    int flags = fcntl(fd_, F_GETFL, 0);
    fcntl(fd_, F_SETFL, flags & ~O_NONBLOCK);
  }

  template <typename T>
  std::future<void> send(const T* buffer, size_t size) {
    return send_impl(reinterpret_cast<const char*>(buffer), size * sizeof(T));
  }

  template <typename T>
  std::future<void> recv(T* buffer, size_t size) {
    return recv_impl(reinterpret_cast<char*>(buffer), size * sizeof(T));
  }

 private:
  struct SocketTask {
    SocketTask(void* b, size_t s, std::promise<void>&& p)
        : buffer(b), size(s), promise(std::move(p)) {}
    SocketTask(SocketTask&& t)
        : buffer(t.buffer), size(t.size), promise(std::move(t.promise)) {}
    void* buffer;
    size_t size;
    std::promise<void> promise;
  };

  void fail_all(const char* reason) {
    failure_ = std::make_exception_ptr(
        std::runtime_error(std::string("[ring] ") + reason));
    for (auto& task : recvs_) {
      task.promise.set_exception(failure_);
    }
    for (auto& task : sends_) {
      task.promise.set_exception(failure_);
    }
    recvs_.clear();
    sends_.clear();
  }

  std::future<void> send_impl(const char* buffer, size_t size) {
    std::promise<void> send_completed_promise;
    auto send_completed_future = send_completed_promise.get_future();
    if (size == 0) {
      send_completed_promise.set_value();
      return send_completed_future;
    }
    {
      std::unique_lock<std::mutex> lock(queue_mutex_);
      if (failure_) {
        send_completed_promise.set_exception(failure_);
        return send_completed_future;
      }
      sends_.emplace_back(SocketTask(
          const_cast<char*>(buffer), size, std::move(send_completed_promise)));
    }
    condition_.notify_one();
    return send_completed_future;
  }

  std::future<void> recv_impl(char* buffer, size_t size) {
    std::promise<void> recv_completed_promise;
    auto recv_completed_future = recv_completed_promise.get_future();
    if (size == 0) {
      recv_completed_promise.set_value();
      return recv_completed_future;
    }
    {
      std::unique_lock<std::mutex> lock(queue_mutex_);
      if (failure_) {
        recv_completed_promise.set_exception(failure_);
        return recv_completed_future;
      }
      recvs_.emplace_back(
          SocketTask(buffer, size, std::move(recv_completed_promise)));
    }
    condition_.notify_one();
    return recv_completed_future;
  }

  bool have_tasks() {
    return !(sends_.empty() && recvs_.empty());
  }

  void worker() {
    constexpr int max_errors = 10;
    int error_count = 0;
    bool peer_closed = false;
    bool delete_recv = false;
    bool delete_send = false;
    while (true) {
      {
        std::unique_lock<std::mutex> lock(queue_mutex_);
        if (delete_recv) {
          recvs_.front().promise.set_value();
          recvs_.pop_front();
          delete_recv = false;
        }
        if (delete_send) {
          sends_.front().promise.set_value();
          sends_.pop_front();
          delete_send = false;
        }
        if (stop_) {
          return;
        }
        if (!have_tasks()) {
          condition_.wait(lock, [this] { return stop_ || have_tasks(); });
          if (stop_) {
            return;
          }
        }
      }

      if (!recvs_.empty()) {
        auto& task = recvs_.front();
        ssize_t r = ::recv(fd_, task.buffer, task.size, 0);
        if (r > 0) {
          task.buffer = static_cast<char*>(task.buffer) + r;
          task.size -= r;
          delete_recv = task.size == 0;
          error_count = 0;
        } else if (r == 0) {
          peer_closed = true;
          log_info(true, "Peer closed socket", fd_);
        } else if (errno != EAGAIN) {
          error_count++;
          log_info(
              true, "Receiving from socket", fd_, "failed with errno", errno);
        }
      }
      if (!sends_.empty()) {
        auto& task = sends_.front();
        ssize_t r = ::send(fd_, task.buffer, task.size, 0);
        if (r > 0) {
          task.buffer = static_cast<char*>(task.buffer) + r;
          task.size -= r;
          delete_send = task.size == 0;
          error_count = 0;
        } else if (errno != EAGAIN) {
          error_count++;
          log_info(true, "Sending to socket", fd_, "failed with errno", errno);
        }
      }

      if (error_count >= max_errors || peer_closed) {
        log_info(true, "Too many send/recv errors. Aborting...");
        std::unique_lock<std::mutex> lock(queue_mutex_);
        fail_all(
            peer_closed ? "peer closed the connection"
                        : "too many socket errors");
        return;
      }
    }
  }

  int fd_;
  bool stop_;
  std::thread worker_;
  std::mutex queue_mutex_;
  std::condition_variable condition_;
  std::list<SocketTask> sends_;
  std::list<SocketTask> recvs_;
  std::exception_ptr failure_;
};

// ---------------------------------------------------------------------------
// Mirror of the patched RingGroup guard
// ---------------------------------------------------------------------------
class Guard {
 public:
  template <typename F>
  void run_guarded(F&& body) {
    try {
      body();
    } catch (...) {
      std::lock_guard<std::mutex> lock(failure_mutex_);
      if (!failure_) {
        failure_ = std::current_exception();
      }
    }
  }

  void check_healthy() {
    std::lock_guard<std::mutex> lock(failure_mutex_);
    if (failure_) {
      std::rethrow_exception(failure_);
    }
  }

 private:
  std::mutex failure_mutex_;
  std::exception_ptr failure_;
};

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------
int failures = 0;

void check(bool ok, const std::string& what) {
  std::cout << (ok ? "  PASS  " : "  FAIL  ") << what << "\n";
  if (!ok) {
    failures++;
  }
}

// Bounded wait, so a regression to the old behaviour is reported rather than
// reproduced.
enum class Outcome { Threw, Completed, HUNG };

Outcome await(std::future<void>& f, int timeout_ms = 3000) {
  if (f.wait_for(std::chrono::milliseconds(timeout_ms)) !=
      std::future_status::ready) {
    return Outcome::HUNG;
  }
  try {
    f.get();
    return Outcome::Completed;
  } catch (const std::exception&) {
    return Outcome::Threw;
  }
}

// A recv waiting on a peer that goes away must fail, not wait forever. This is
// the exact scenario that wedged the ring: an iOS app being suspended closes
// its sockets cleanly, so recv() returns 0.
void test_peer_close_fails_pending_recv() {
  std::cout << "peer closes connection with a recv outstanding\n";
  int sv[2];
  socketpair(AF_UNIX, SOCK_STREAM, 0, sv);

  char buffer[64] = {0};
  {
    SocketThread thread(sv[0]);
    auto f = thread.recv(buffer, sizeof(buffer));

    std::this_thread::sleep_for(std::chrono::milliseconds(50));
    close(sv[1]); // peer disappears

    auto outcome = await(f);
    check(outcome != Outcome::HUNG, "future becomes ready (does not hang)");
    check(outcome == Outcome::Threw, "future reports the failure");
  }
  close(sv[0]);
}

// Once the worker has given up, nothing drains the queue -- so new work must be
// rejected immediately rather than enqueued forever.
void test_work_after_failure_is_rejected_immediately() {
  std::cout << "work submitted after the socket died\n";
  int sv[2];
  socketpair(AF_UNIX, SOCK_STREAM, 0, sv);

  char buffer[64] = {0};
  {
    SocketThread thread(sv[0]);
    auto first = thread.recv(buffer, sizeof(buffer));
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
    close(sv[1]);
    await(first);

    auto later = thread.recv(buffer, sizeof(buffer));
    auto outcome = await(later, 1000);
    check(outcome != Outcome::HUNG, "late recv does not hang");
    check(outcome == Outcome::Threw, "late recv fails immediately");

    auto sent = thread.send(buffer, sizeof(buffer));
    check(await(sent, 1000) == Outcome::Threw, "late send fails immediately");
  }
  close(sv[0]);
}

// A healthy socket must be entirely unaffected.
void test_healthy_transfer_still_works() {
  std::cout << "healthy socket\n";
  int sv[2];
  socketpair(AF_UNIX, SOCK_STREAM, 0, sv);

  const char* payload = "hello ring";
  size_t n = std::strlen(payload) + 1;
  char received[64] = {0};
  {
    SocketThread sender(sv[0]);
    SocketThread receiver(sv[1]);
    auto s = sender.send(payload, n);
    auto r = receiver.recv(received, n);
    check(await(s) == Outcome::Completed, "send completes");
    check(await(r) == Outcome::Completed, "recv completes");
    check(std::strcmp(received, payload) == 0, "payload arrives intact");
  }
  close(sv[0]);
  close(sv[1]);
}

// The guard must swallow the exception where it would otherwise unwind a
// scheduler thread and call std::terminate, then re-raise it on a thread the
// caller controls.
void test_guard_defers_the_throw_to_the_caller() {
  std::cout << "guard defers a scheduler-thread failure to the caller\n";
  Guard guard;

  bool escaped = false;
  std::thread scheduler([&]() {
    try {
      guard.run_guarded([]() { throw std::runtime_error("[ring] boom"); });
    } catch (...) {
      escaped = true; // would have been std::terminate in the real dispatch
    }
  });
  scheduler.join();
  check(!escaped, "nothing escapes the dispatched task");

  bool threw = false;
  std::string message;
  try {
    guard.check_healthy();
  } catch (const std::exception& e) {
    threw = true;
    message = e.what();
  }
  check(threw, "check_healthy rethrows on the calling thread");
  check(message == "[ring] boom", "the original failure is preserved");

  // Sticky: the ring stays broken until it is rebuilt.
  bool again = false;
  try {
    guard.check_healthy();
  } catch (const std::exception&) {
    again = true;
  }
  check(again, "failure is sticky across calls");
}

void test_guard_keeps_the_first_failure() {
  std::cout << "guard keeps the first failure\n";
  Guard guard;
  guard.run_guarded([]() { throw std::runtime_error("first"); });
  guard.run_guarded([]() { throw std::runtime_error("second"); });

  std::string message;
  try {
    guard.check_healthy();
  } catch (const std::exception& e) {
    message = e.what();
  }
  check(message == "first", "the first failure names what broke the ring");
}

// ---------------------------------------------------------------------------
// The bug, pinned
// ---------------------------------------------------------------------------
// Mirror of the UNPATCHED worker, so the defect is demonstrated rather than
// merely described. The only differences from the class above are the ones the
// patch introduces: no failure_ state, r == 0 not distinguished, and the abort
// path returning while leaving promises unfulfilled.
class LegacySocketThread {
 public:
  LegacySocketThread(int fd) : fd_(fd), stop_(false) {
    worker_ = std::thread(&LegacySocketThread::worker, this);
    int flags = fcntl(fd, F_GETFL, 0);
    fcntl(fd, F_SETFL, flags | O_NONBLOCK);
  }
  ~LegacySocketThread() {
    stop_ = true;
    condition_.notify_all();
    worker_.join();
  }

  std::future<void> recv(char* buffer, size_t size) {
    std::promise<void> p;
    auto f = p.get_future();
    {
      std::unique_lock<std::mutex> lock(queue_mutex_);
      tasks_.emplace_back(Task{buffer, size, std::move(p)});
    }
    condition_.notify_one();
    return f;
  }

 private:
  struct Task {
    void* buffer;
    size_t size;
    std::promise<void> promise;
  };

  void worker() {
    int error_count = 0;
    bool done = false;
    while (true) {
      {
        std::unique_lock<std::mutex> lock(queue_mutex_);
        if (done) {
          tasks_.front().promise.set_value();
          tasks_.pop_front();
          done = false;
        }
        if (stop_) {
          return;
        }
        if (tasks_.empty()) {
          condition_.wait(lock, [this] { return stop_ || !tasks_.empty(); });
          if (stop_) {
            return;
          }
        }
      }
      if (!tasks_.empty()) {
        auto& task = tasks_.front();
        ssize_t r = ::recv(fd_, task.buffer, task.size, 0);
        if (r > 0) {
          task.buffer = static_cast<char*>(task.buffer) + r;
          task.size -= r;
          done = task.size == 0;
          error_count = 0;
        } else if (errno != EAGAIN) {
          error_count++;
        }
      }
      if (error_count >= 10) {
        return; // promises abandoned, still unfulfilled and undestroyed
      }
    }
  }

  int fd_;
  bool stop_;
  std::thread worker_;
  std::mutex queue_mutex_;
  std::condition_variable condition_;
  std::list<Task> tasks_;
};

void test_unpatched_behaviour_hangs() {
  std::cout << "unpatched worker, same scenario (pins the defect)\n";
  int sv[2];
  socketpair(AF_UNIX, SOCK_STREAM, 0, sv);

  char buffer[64] = {0};
  {
    LegacySocketThread thread(sv[0]);
    auto f = thread.recv(buffer, sizeof(buffer));
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
    close(sv[1]);

    auto outcome = await(f, 1500);
    check(
        outcome == Outcome::HUNG,
        "unpatched: future never becomes ready -- this is the bug the patch fixes");
  }
  close(sv[0]);
}

} // namespace

int main() {
  test_healthy_transfer_still_works();
  test_peer_close_fails_pending_recv();
  test_work_after_failure_is_rejected_immediately();
  test_guard_defers_the_throw_to_the_caller();
  test_guard_keeps_the_first_failure();
  test_unpatched_behaviour_hangs();

  std::cout << "\n" << (failures == 0 ? "all checks passed" : "FAILURES: ")
            << (failures == 0 ? "" : std::to_string(failures)) << "\n";
  return failures == 0 ? 0 : 1;
}
