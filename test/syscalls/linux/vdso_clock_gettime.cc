// Copyright 2018 The gVisor Authors.
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

#include <sched.h>
#include <stdint.h>
#include <sys/time.h>
#include <syscall.h>
#include <time.h>
#include <unistd.h>

#include <array>
#include <atomic>
#include <map>
#include <ostream>
#include <sstream>
#include <string>
#include <utility>

#include "gmock/gmock.h"
#include "gtest/gtest.h"
#include "absl/strings/numbers.h"
#include "absl/strings/str_cat.h"
#include "absl/time/clock.h"
#include "absl/time/time.h"
#include "test/util/test_util.h"
#include "test/util/thread_util.h"

#if defined(__x86_64__)
#include <cpuid.h>
#endif

namespace gvisor {
namespace testing {

namespace {

std::string PrintClockId(::testing::TestParamInfo<clockid_t> info) {
  switch (info.param) {
    case CLOCK_MONOTONIC:
      return "CLOCK_MONOTONIC";
    case CLOCK_BOOTTIME:
      return "CLOCK_BOOTTIME";
    default:
      return absl::StrCat(info.param);
  }
}

class MonotonicVDSOClockTest : public ::testing::TestWithParam<clockid_t> {};

// Fork-only native control. The run_under wrapper explicitly selects this
// disabled fixture before and after the original uninstrumented clock owner.
struct CycleSample {
  uint64_t ticks = 0;
  uint32_t aux = 0;
};

CycleSample ReadCycles() {
  CycleSample result;
#if defined(__x86_64__)
  uint32_t lo, hi;
  // RDTSCP orders prior loads; LFENCE orders subsequent instructions. AUX is
  // retained as the guest kernel's raw CPU signature, not a physical CPU ID.
  asm volatile("rdtscp; lfence"
               : "=a"(lo), "=d"(hi), "=c"(result.aux)
               :
               : "memory");
  result.ticks = (static_cast<uint64_t>(hi) << 32) | lo;
#endif
  return result;
}

// Counter anomalies are diagnostic data, independent of the clock assertions.
// Keep only the first examples of each kind and format them after the loop.
struct CycleEvent {
  CycleSample previous;
  CycleSample current;
  const char* previous_point = nullptr;
  const char* current_point = nullptr;
};

class CycleObserver {
 public:
  uint64_t Record(CycleSample sample, const char* point) {
    uint64_t reverse_ticks = 0;
    if (samples != 0) {
      if (sample.ticks < previous_.ticks) {
        reverse_ticks = previous_.ticks - sample.ticks;
        if (reverse_ticks > max_reverse_ticks) {
          max_reverse_ticks = reverse_ticks;
        }
        Capture(regression_examples_, regressions, sample, point);
      }
      if (sample.aux != previous_.aux) {
        Capture(aux_examples_, aux_changes, sample, point);
      }
    }
    previous_ = sample;
    previous_point_ = point;
    ++samples;
    return reverse_ticks;
  }

  std::string RegressionExamples() const {
    return Format(regression_examples_, regressions);
  }
  std::string AuxExamples() const { return Format(aux_examples_, aux_changes); }

  uint64_t samples = 0;
  uint64_t regressions = 0;
  uint64_t max_reverse_ticks = 0;
  uint64_t aux_changes = 0;

 private:
  static constexpr size_t kExampleLimit = 8;
  using Examples = std::array<CycleEvent, kExampleLimit>;

  void Capture(Examples& examples, uint64_t& count, CycleSample current,
               const char* point) {
    if (count < examples.size()) {
      auto& event = examples[count];
      event.previous = previous_;
      event.current = current;
      event.previous_point = previous_point_;
      event.current_point = point;
    }
    ++count;
  }

  static std::string Format(const Examples& examples, uint64_t count) {
    std::ostringstream out;
    for (size_t i = 0; i < examples.size() && i < count; ++i) {
      const auto& event = examples[i];
      out << "previous=" << event.previous_point
          << " ticks=" << event.previous.ticks << " aux=" << event.previous.aux
          << " current=" << event.current_point
          << " ticks=" << event.current.ticks << " aux=" << event.current.aux
          << '\n';
    }
    return out.str();
  }

  CycleSample previous_;
  const char* previous_point_ = nullptr;
  Examples regression_examples_{};
  Examples aux_examples_{};
};

TEST(DISABLED_NativeCycleHandoffTest, SerializedCounters) {
  SKIP_IF(IsRunningOnGvisor());
#if defined(__x86_64__)
  unsigned eax, ebx, ecx, edx;
  ASSERT_TRUE(__get_cpuid(0x80000001, &eax, &ebx, &ecx, &edx));
  ASSERT_NE(edx & (1u << 27), 0u) << "Diagnostic requires RDTSCP";
  cpu_set_t allowed;
  ASSERT_THAT(sched_getaffinity(0, sizeof(allowed), &allowed),
              SyscallSucceeds());
  ASSERT_GE(CPU_COUNT(&allowed), 2);
  std::array<int, 2> cpus;
  size_t selected = 0;
  for (int cpu = 0; cpu < CPU_SETSIZE && selected < cpus.size(); ++cpu) {
    if (CPU_ISSET(cpu, &allowed)) {
      cpus[selected++] = cpu;
    }
  }
  ASSERT_EQ(selected, cpus.size());

  // The token serializes both the samples and observer updates. Publishing a
  // sample happens before the peer acquires the token and reads its counter.
  // Only these two temporary threads change affinity; the test thread does not.
  std::atomic<unsigned> turn{0};
  std::atomic<bool> stop{false};
  CycleObserver path;
  // Index by the destination side of the token handoff.
  std::array<uint64_t, 2> direction_regressions{};
  std::array<uint64_t, 2> direction_max_reverse_ticks{};
  auto sample = [&](unsigned side) {
    cpu_set_t pinned;
    CPU_ZERO(&pinned);
    CPU_SET(cpus[side], &pinned);
    ASSERT_THAT(sched_setaffinity(0, sizeof(pinned), &pinned),
                SyscallSucceeds());
    cpu_set_t effective;
    ASSERT_THAT(sched_getaffinity(0, sizeof(effective), &effective),
                SyscallSucceeds());
    ASSERT_TRUE(CPU_EQUAL(&pinned, &effective));
    ASSERT_EQ(sched_getcpu(), cpus[side]);
    while (!stop.load(std::memory_order_relaxed)) {
      if (turn.load(std::memory_order_acquire) != side) {
        asm volatile("pause");
        continue;
      }
      const uint64_t reverse_ticks =
          path.Record(ReadCycles(), side == 0 ? "first_cpu" : "second_cpu");
      if (reverse_ticks != 0) {
        ++direction_regressions[side];
        if (reverse_ticks > direction_max_reverse_ticks[side]) {
          direction_max_reverse_ticks[side] = reverse_ticks;
        }
      }
      turn.store(1 - side, std::memory_order_release);
    }
  };
  ScopedThread first([&] { sample(0); });
  ScopedThread second([&] { sample(1); });
  absl::SleepFor(absl::Seconds(2));
  stop.store(true, std::memory_order_relaxed);
  first.Join();
  second.Join();

  RecordProperty("native_handoff_first_cpu", std::to_string(cpus[0]));
  RecordProperty("native_handoff_second_cpu", std::to_string(cpus[1]));
  RecordProperty("native_handoff_allowed_cpus", CPU_COUNT(&allowed));
  RecordProperty("native_handoff_cycle_samples", std::to_string(path.samples));
  RecordProperty("native_handoff_cycle_regressions",
                 std::to_string(path.regressions));
  RecordProperty("native_handoff_max_reverse_ticks",
                 std::to_string(path.max_reverse_ticks));
  for (unsigned side = 0; side < cpus.size(); ++side) {
    const std::string prefix = side == 0 ? "native_handoff_second_to_first_"
                                         : "native_handoff_first_to_second_";
    RecordProperty(prefix + "regressions",
                   std::to_string(direction_regressions[side]));
    RecordProperty(prefix + "max_reverse_ticks",
                   std::to_string(direction_max_reverse_ticks[side]));
  }
  RecordProperty("native_handoff_aux_changes",
                 std::to_string(path.aux_changes));
  RecordProperty("native_handoff_regression_examples",
                 path.RegressionExamples());
  RecordProperty("native_handoff_aux_examples", path.AuxExamples());
  ASSERT_GT(path.samples, 1u);
  // Counter reversals are diagnostic data. The original clock assertions below
  // remain independent; a passing handoff only covers this pair and its
  // latency.
#else
  GTEST_SKIP() << "Serialized cycle diagnostic requires AMD64";
#endif
}

TEST_P(MonotonicVDSOClockTest, IsCorrect) {
  // The VDSO implementation of clock_gettime() uses the TSC. On KVM, sentry and
  // application TSCs can be very desynchronized; see
  // sentry/platform/kvm/kvm.vCPU.setSystemTime().
  SKIP_IF(GvisorPlatform() == Platform::kKVM);

  // Check that when we alternate readings from the clock_gettime syscall and
  // the VDSO's implementation, we observe the combined sequence as being
  // monotonic.
  struct timespec tvdso, tsys;
  absl::Time vdso_time, sys_time;
  ASSERT_THAT(syscall(__NR_clock_gettime, GetParam(), &tsys),
              SyscallSucceeds());
  sys_time = absl::TimeFromTimespec(tsys);
  auto end = absl::Now() + absl::Seconds(10);
  while (absl::Now() < end) {
    ASSERT_THAT(clock_gettime(GetParam(), &tvdso), SyscallSucceeds());
    vdso_time = absl::TimeFromTimespec(tvdso);
    EXPECT_LE(sys_time, vdso_time);
    ASSERT_THAT(syscall(__NR_clock_gettime, GetParam(), &tsys),
                SyscallSucceeds());
    sys_time = absl::TimeFromTimespec(tsys);
    EXPECT_LE(vdso_time, sys_time);
  }
}

INSTANTIATE_TEST_SUITE_P(ClockGettime, MonotonicVDSOClockTest,
                         ::testing::Values(CLOCK_MONOTONIC, CLOCK_BOOTTIME),
                         PrintClockId);

}  // namespace

}  // namespace testing
}  // namespace gvisor
