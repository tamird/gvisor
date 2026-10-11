// Copyright 2026 The gVisor Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include <pthread.h>
#include <sched.h>
#include <stdint.h>

#include <array>
#include <atomic>
#include <vector>

#include "gtest/gtest.h"
#include "absl/base/attributes.h"
#include "test/util/thread_util.h"

// Stable symbols let the diagnostic bind the actual compiled test window.
extern "C" ABSL_ATTRIBUTE_NOINLINE uint64_t
AtomicStoreLoad(std::atomic<uint64_t>* own, std::atomic<uint64_t>* peer) {
  own->store(1, std::memory_order_seq_cst);
  return peer->load(std::memory_order_seq_cst);
}

extern "C" ABSL_ATTRIBUTE_NOINLINE uint64_t
AtomicStoreFenceLoad(std::atomic<uint64_t>* own, std::atomic<uint64_t>* peer) {
  own->store(1, std::memory_order_seq_cst);
  std::atomic_thread_fence(std::memory_order_seq_cst);
  return peer->load(std::memory_order_seq_cst);
}

namespace gvisor {
namespace testing {
namespace {

constexpr int kRounds = 100000;
static_assert(std::atomic<uint64_t>::is_always_lock_free);

// Keep the tested words 4096 bytes apart, well beyond Cortex-A57 cache lines,
// so sharing a line cannot serialize the store/load window.
struct alignas(4096) Cell {
  std::atomic<uint64_t> value{0};
};

using Probe = uint64_t (*)(std::atomic<uint64_t>*, std::atomic<uint64_t>*);

void WaitForPeer(pthread_barrier_t* barrier) {
  const int result = pthread_barrier_wait(barrier);
  EXPECT_TRUE(result == 0 || result == PTHREAD_BARRIER_SERIAL_THREAD) << result;
}

void CheckStoreBuffering(Probe probe) {
  cpu_set_t allowed;
  CPU_ZERO(&allowed);
  ASSERT_EQ(sched_getaffinity(0, sizeof(allowed), &allowed), 0);
  std::array<int, 2> cpus = {-1, -1};
  int found = 0;
  for (int cpu = 0; cpu < CPU_SETSIZE && found < 2; ++cpu) {
    if (CPU_ISSET(cpu, &allowed)) {
      cpus[found++] = cpu;
    }
  }
  ASSERT_EQ(found, 2);

  std::array<Cell, 2> cells;
  std::array<std::vector<uint64_t>, 2> observations = {
      std::vector<uint64_t>(kRounds), std::vector<uint64_t>(kRounds)};
  pthread_barrier_t barrier;
  ASSERT_EQ(pthread_barrier_init(&barrier, nullptr, 2), 0);

  auto worker = [&](int index) {
    cpu_set_t affinity;
    CPU_ZERO(&affinity);
    CPU_SET(cpus[index], &affinity);
    EXPECT_EQ(sched_setaffinity(0, sizeof(affinity), &affinity), 0);
    cpu_set_t effective;
    CPU_ZERO(&effective);
    EXPECT_EQ(sched_getaffinity(0, sizeof(effective), &effective), 0);
    EXPECT_TRUE(CPU_EQUAL(&affinity, &effective));
    for (int round = 0; round < kRounds; ++round) {
      cells[index].value.store(0, std::memory_order_relaxed);
      // Both resets finish before either tested pair. The second barrier
      // prevents a subsequent reset from overlapping either tested load.
      WaitForPeer(&barrier);
      observations[index][round] =
          probe(&cells[index].value, &cells[1 - index].value);
      WaitForPeer(&barrier);
    }
  };
  ScopedThread first([&] { worker(0); });
  ScopedThread second([&] { worker(1); });
  first.Join();
  second.Join();
  EXPECT_EQ(pthread_barrier_destroy(&barrier), 0);

  int both_zero = 0;
  int invalid = 0;
  for (int round = 0; round < kRounds; ++round) {
    const uint64_t a = observations[0][round];
    const uint64_t b = observations[1][round];
    both_zero += a == 0 && b == 0;
    invalid += a > 1 || b > 1;
  }
  ::testing::Test::RecordProperty("rounds", kRounds);
  ::testing::Test::RecordProperty("cpu_0", cpus[0]);
  ::testing::Test::RecordProperty("cpu_1", cpus[1]);
  ::testing::Test::RecordProperty("both_zero", both_zero);
  ::testing::Test::RecordProperty("invalid_values", invalid);
  EXPECT_EQ(invalid, 0);
  // Sequential consistency forbids both loads seeing the initial value.
  EXPECT_EQ(both_zero, 0);
}

TEST(AtomicOrderingTest, SequentiallyConsistentStoreLoad) {
  CheckStoreBuffering(AtomicStoreLoad);
}

TEST(AtomicOrderingTest, SequentiallyConsistentStoreFenceLoad) {
  CheckStoreBuffering(AtomicStoreFenceLoad);
}

}  // namespace
}  // namespace testing
}  // namespace gvisor
