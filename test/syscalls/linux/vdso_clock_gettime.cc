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

#include <elf.h>
#include <sched.h>
#include <stdint.h>
#include <sys/auxv.h>
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
#include "vdso/params.h"

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

// This diagnostic reads gVisor's shared parameter page only on AMD64. Keep the
// ordinary clock calls and assertions below; collect records without I/O and
// format failed clock pairs only when an existing ordering assertion fails.
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

struct ParameterSample {
  bool available = false;
  uint64_t sequence_before = 0;
  uint64_t ready = 0;
  int64_t base_cycles = 0;
  int64_t base_ref = 0;
  uint64_t frequency = 0;
  uint64_t sequence_after = 0;
};

ParameterSample ReadParameters(const params* page) {
  ParameterSample result;
  result.available = true;
  result.sequence_before = __atomic_load_n(&page->seq_count, __ATOMIC_ACQUIRE);
  result.ready = __atomic_load_n(&page->monotonic_ready, __ATOMIC_RELAXED);
  result.base_cycles =
      __atomic_load_n(&page->monotonic_base_cycles, __ATOMIC_RELAXED);
  result.base_ref =
      __atomic_load_n(&page->monotonic_base_ref, __ATOMIC_RELAXED);
  result.frequency =
      __atomic_load_n(&page->monotonic_frequency, __ATOMIC_RELAXED);
  // Prevent the sequence read from moving before the parameter loads. Unlike
  // a VDSO reader, this observer does not retry: odd/changed sequences remain
  // visible as unusable snapshots and do not alter the original call sequence.
  asm volatile("" : : : "memory");
  result.sequence_after = __atomic_load_n(&page->seq_count, __ATOMIC_ACQUIRE);
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
  CycleSample Read(const char* point) {
    const CycleSample sample = ReadCycles();
    Record(sample, point);
    return sample;
  }

  void Record(CycleSample sample, const char* point) {
    if (samples != 0) {
      if (sample.ticks < previous_.ticks) {
        Capture(regression_examples_, regressions, sample, point);
      }
      if (sample.aux != previous_.aux) {
        Capture(aux_examples_, aux_changes, sample, point);
      }
    }
    previous_ = sample;
    previous_point_ = point;
    ++samples;
  }

  std::string RegressionExamples() const {
    return Format(regression_examples_, regressions);
  }
  std::string AuxExamples() const { return Format(aux_examples_, aux_changes); }

  uint64_t samples = 0;
  uint64_t regressions = 0;
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

TEST(NativeCycleHandoffTest, SerializedCounters) {
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
      path.Record(ReadCycles(), side == 0 ? "first_cpu" : "second_cpu");
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

// Fork-only output from the explicitly marked clock_gettime request. The
// kernel fills every scalar; native calls never request this extension.
constexpr uint64_t kInternalClockMagic = 0x475653434c4f434b;
struct InternalClockSample {
  uint64_t magic = 0;
  uint64_t before_cycles = 0;
  uint64_t before_aux = 0;
  uint64_t after_cycles = 0;
  uint64_t after_aux = 0;
  uint64_t seconds = 0;
  uint64_t nanoseconds = 0;

  bool Matches(const timespec& ts) const {
    return magic == kInternalClockMagic &&
           seconds == static_cast<uint64_t>(ts.tv_sec) &&
           nanoseconds == static_cast<uint64_t>(ts.tv_nsec);
  }
};
static_assert(sizeof(InternalClockSample) == 7 * sizeof(uint64_t));

std::ostream& operator<<(std::ostream& out, const InternalClockSample& sample) {
  if (sample.magic != kInternalClockMagic) {
    return out << "{unavailable}";
  }
  return out << "{cycles_before=" << sample.before_cycles
             << ",aux_before=" << sample.before_aux
             << ",cycles_after=" << sample.after_cycles
             << ",aux_after=" << sample.after_aux
             << ",seconds=" << sample.seconds
             << ",nanoseconds=" << sample.nanoseconds << "}";
}

struct ReadSample {
  bool active = false;
  ParameterSample params_before;
  CycleSample cycles_before;
  CycleSample cycles_after;
  ParameterSample params_after;
  InternalClockSample internal;

  long ClockSyscall(clockid_t clock, timespec* ts, bool capture_internal) {
    internal = {};
    if (capture_internal) {
      return syscall(__NR_clock_gettime, clock, ts, &internal,
                     kInternalClockMagic);
    }
    return syscall(__NR_clock_gettime, clock, ts);
  }

  // Record the causal syscall path using samples that have already returned.
  // This adds no counter reads or Sentry work. Raw AUX remains an opaque value.
  void RecordInternalPath(CycleObserver* observer) const {
    observer->Record(cycles_before, "caller.before");
    observer->Record(
        {internal.before_cycles, static_cast<uint32_t>(internal.before_aux)},
        "sentry.before");
    observer->Record(
        {internal.after_cycles, static_cast<uint32_t>(internal.after_aux)},
        "sentry.after");
    observer->Record(cycles_after, "caller.after");
  }

  void Before(const params* page, CycleObserver* observer, const char* point) {
    if (observer != nullptr) {
      active = true;
      if (page != nullptr) {
        params_before = ReadParameters(page);
      }
      cycles_before = observer->Read(point);
    }
  }
  void After(const params* page, CycleObserver* observer, const char* point) {
    if (observer != nullptr) {
      cycles_after = observer->Read(point);
      if (page != nullptr) {
        params_after = ReadParameters(page);
      }
    }
  }
};

std::ostream& operator<<(std::ostream& out, const ParameterSample& sample) {
  if (!sample.available) {
    return out << "{unavailable}";
  }
  return out << "{seq_before=" << sample.sequence_before
             << ",ready=" << sample.ready
             << ",base_cycles=" << sample.base_cycles
             << ",base_ref_ns=" << sample.base_ref
             << ",frequency_hz=" << sample.frequency
             << ",seq_after=" << sample.sequence_after << "}";
}

std::ostream& operator<<(std::ostream& out, const ReadSample& sample) {
  if (!sample.active) {
    return out << "{unavailable}";
  }
  return out << "{params_before=" << sample.params_before
             << ",cycles_before=" << sample.cycles_before.ticks
             << ",aux_before=" << sample.cycles_before.aux
             << ",cycles_after=" << sample.cycles_after.ticks
             << ",aux_after=" << sample.cycles_after.aux
             << ",params_after=" << sample.params_after
             << ",internal=" << sample.internal << "}";
}

TEST_P(MonotonicVDSOClockTest, IsCorrect) {
  // The VDSO implementation of clock_gettime() uses the TSC. On KVM, sentry and
  // application TSCs can be very desynchronized; see
  // sentry/platform/kvm/kvm.vCPU.setSystemTime().
  SKIP_IF(GvisorPlatform() == Platform::kKVM);

  // Check that when we alternate readings from the clock_gettime syscall and
  // the VDSO's implementation, we observe the combined sequence as being
  // monotonic.
  const params* page = nullptr;
  CycleObserver cycle_observer;
  CycleObserver* observer = nullptr;
#if defined(__x86_64__)
  unsigned eax, ebx, ecx, edx;
  ASSERT_TRUE(__get_cpuid(0x80000001, &eax, &ebx, &ecx, &edx));
  ASSERT_NE(edx & (1u << 27), 0u) << "Diagnostic requires RDTSCP";
  observer = &cycle_observer;
  RecordProperty("caller_cycle_observer", "amd64_rdtscp");
  if (IsRunningOnGvisor()) {
    const uintptr_t base = getauxval(AT_SYSINFO_EHDR);
    ASSERT_NE(base, 0u);
    // vdso_amd64.lds places _params one 4 KiB page before the ELF header.
    ASSERT_EQ(kPageSize, 4096u);
    page = reinterpret_cast<const params*>(base - kPageSize);
    RecordProperty("clock_pair_observer", "gvisor_amd64_rdtscp");
  }
#endif
  ReadSample syscall_sample, vdso_sample;
  struct timespec tvdso, tsys;
  absl::Time vdso_time, sys_time;
  uint64_t internal_samples = 0;
  uint64_t internal_matches = 0;
  CycleObserver internal_path;
  syscall_sample.Before(page, observer, "syscall.before");
  ASSERT_THAT(syscall_sample.ClockSyscall(GetParam(), &tsys, page != nullptr),
              SyscallSucceeds());
  syscall_sample.After(page, observer, "syscall.after");
  if (page != nullptr) {
    ++internal_samples;
    if (syscall_sample.internal.Matches(tsys)) {
      ++internal_matches;
      syscall_sample.RecordInternalPath(&internal_path);
    }
  }
  sys_time = absl::TimeFromTimespec(tsys);
  auto end = absl::Now() + absl::Seconds(10);
  while (absl::Now() < end) {
    vdso_sample.Before(page, observer, "vdso.before");
    ASSERT_THAT(clock_gettime(GetParam(), &tvdso), SyscallSucceeds());
    vdso_sample.After(page, observer, "vdso.after");
    vdso_time = absl::TimeFromTimespec(tvdso);
    EXPECT_LE(sys_time, vdso_time)
        << "clock_pair syscall_to_vdso syscall=" << syscall_sample
        << " vdso=" << vdso_sample;
    syscall_sample.Before(page, observer, "syscall.before");
    ASSERT_THAT(syscall_sample.ClockSyscall(GetParam(), &tsys, page != nullptr),
                SyscallSucceeds());
    syscall_sample.After(page, observer, "syscall.after");
    if (page != nullptr) {
      ++internal_samples;
      if (syscall_sample.internal.Matches(tsys)) {
        ++internal_matches;
        syscall_sample.RecordInternalPath(&internal_path);
      }
    }
    sys_time = absl::TimeFromTimespec(tsys);
    EXPECT_LE(vdso_time, sys_time)
        << "clock_pair vdso_to_syscall vdso=" << vdso_sample
        << " syscall=" << syscall_sample;
  }
  if (page != nullptr) {
    RecordProperty("internal_clock_samples", std::to_string(internal_samples));
    RecordProperty("internal_clock_matches", std::to_string(internal_matches));
    RecordProperty("internal_path_cycle_samples",
                   std::to_string(internal_path.samples));
    RecordProperty("internal_path_cycle_regressions",
                   std::to_string(internal_path.regressions));
    RecordProperty("internal_path_aux_changes",
                   std::to_string(internal_path.aux_changes));
    RecordProperty("internal_path_regression_examples",
                   internal_path.RegressionExamples());
    RecordProperty("internal_path_aux_examples", internal_path.AuxExamples());
    EXPECT_EQ(internal_matches, internal_samples)
        << "Internal diagnostic must match each returned timespec";
  }
  if (observer != nullptr) {
    RecordProperty("caller_cycle_samples", std::to_string(observer->samples));
    RecordProperty("caller_cycle_regressions",
                   std::to_string(observer->regressions));
    RecordProperty("caller_cycle_aux_changes",
                   std::to_string(observer->aux_changes));
    RecordProperty("caller_cycle_regression_examples",
                   observer->RegressionExamples());
    RecordProperty("caller_cycle_aux_examples", observer->AuxExamples());
  }
}

INSTANTIATE_TEST_SUITE_P(ClockGettime, MonotonicVDSOClockTest,
                         ::testing::Values(CLOCK_MONOTONIC, CLOCK_BOOTTIME),
                         PrintClockId);

}  // namespace

}  // namespace testing
}  // namespace gvisor
