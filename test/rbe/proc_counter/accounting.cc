// Copyright 2026 The gVisor Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include <errno.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <stddef.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <sys/utsname.h>
#include <unistd.h>

#include <condition_variable>
#include <cstdio>
#include <fstream>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>

namespace {

int FilterCount() {
  std::ifstream status("/proc/thread-self/status");
  for (std::string line; std::getline(status, line);) {
    if (line.compare(0, 16, "Seccomp_filters:") == 0) {
      std::istringstream in(line.substr(16));
      int count;
      if (in >> count && count >= 0) return count;
    }
  }
  return -1;
}

// The only syscall this process-local filter changes is an ID query. EDOM
// distinguishes its result from the normal positive parent PID on this worker.
bool Install() {
  sock_filter filter[] = {
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, nr)),
      BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_getppid, 0, 1),
      BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EDOM),
      BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
  };
  sock_fprog prog = {sizeof(filter) / sizeof(filter[0]), filter};
  return syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER,
                 SECCOMP_FILTER_FLAG_TSYNC, &prog) == 0;
}

struct Sample {
  bool query;
  int count;
};

Sample Before() {
  return {syscall(SYS_getppid) > 0 && syscall(SYS_getpid) > 0, FilterCount()};
}

Sample After() {
  return {syscall(SYS_getppid) == -1 && errno == EDOM && syscall(SYS_getpid) > 0,
          FilterCount()};
}

}  // namespace

int main() {
  struct utsname kernel = {};
  if (uname(&kernel) != 0 || prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) return 1;
  const Sample main_before = Before();
  Sample peer_before{}, peer_after{};
  std::mutex mutex;
  std::condition_variable changed;
  bool ready = false, installed = false;
  std::thread peer([&]() {
    peer_before = Before();
    std::unique_lock<std::mutex> lock(mutex);
    ready = true;
    changed.notify_one();
    changed.wait(lock, [&]() { return installed; });
    peer_after = After();
  });
  std::unique_lock<std::mutex> lock(mutex);
  changed.wait(lock, [&]() { return ready; });
  bool sync_ok = Install();
  const Sample main_after = After();
  installed = true;
  lock.unlock();
  changed.notify_one();
  peer.join();
#if defined(__aarch64__)
  bool native = std::string(kernel.machine) == "aarch64";
#elif defined(__x86_64__)
  bool native = std::string(kernel.machine) == "x86_64";
#else
#error Unsupported probe architecture
#endif
  bool before_ok = main_before.query && peer_before.query;
  bool enforced = main_after.query && peer_after.query;
  bool observable = main_before.count >= 0 && peer_before.count >= 0 &&
                    main_after.count >= 0 && peer_after.count >= 0;
  std::printf("ACCOUNTING kernel=%s native=%d before=%d installed=%d enforced=%d "
              "observable=%d before_equal=%d after_equal=%d both_incremented=%d\n",
              kernel.release, native, before_ok, sync_ok, enforced, observable,
              main_before.count == peer_before.count,
              main_after.count == peer_after.count,
              main_after.count == main_before.count + 1 &&
              peer_after.count == peer_before.count + 1);
  return native && before_ok && sync_ok && enforced && observable ? 0 : 1;
}
