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

#include <stdint.h>
#include <sys/auxv.h>
#include <sys/time.h>
#include <syscall.h>
#include <time.h>
#include <unistd.h>

#include <map>
#include <ostream>
#include <string>
#include <utility>

#include "gmock/gmock.h"
#include "gtest/gtest.h"
#include "absl/strings/numbers.h"
#include "absl/strings/str_cat.h"
#include "absl/time/clock.h"
#include "absl/time/time.h"
#include "test/util/test_util.h"
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
// format them only when an existing ordering assertion fails.
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
  uint64_t sequence_before = 0;
  uint64_t ready = 0;
  int64_t base_cycles = 0;
  int64_t base_ref = 0;
  uint64_t frequency = 0;
  uint64_t sequence_after = 0;
};

ParameterSample ReadParameters(const params* page) {
  ParameterSample result;
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

struct ReadSample {
  bool active = false;
  ParameterSample params_before;
  CycleSample cycles_before;
  CycleSample cycles_after;
  ParameterSample params_after;

  void Before(const params* page) {
    if (page != nullptr) {
      active = true;
      params_before = ReadParameters(page);
      cycles_before = ReadCycles();
    }
  }
  void After(const params* page) {
    if (page != nullptr) {
      cycles_after = ReadCycles();
      params_after = ReadParameters(page);
    }
  }
};

std::ostream& operator<<(std::ostream& out, const ParameterSample& sample) {
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
             << ",params_after=" << sample.params_after << "}";
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
#if defined(__x86_64__)
  if (IsRunningOnGvisor()) {
    unsigned eax, ebx, ecx, edx;
    ASSERT_TRUE(__get_cpuid(0x80000001, &eax, &ebx, &ecx, &edx));
    ASSERT_NE(edx & (1u << 27), 0u) << "Diagnostic requires RDTSCP";
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
  syscall_sample.Before(page);
  ASSERT_THAT(syscall(__NR_clock_gettime, GetParam(), &tsys),
              SyscallSucceeds());
  syscall_sample.After(page);
  sys_time = absl::TimeFromTimespec(tsys);
  auto end = absl::Now() + absl::Seconds(10);
  while (absl::Now() < end) {
    vdso_sample.Before(page);
    ASSERT_THAT(clock_gettime(GetParam(), &tvdso), SyscallSucceeds());
    vdso_sample.After(page);
    vdso_time = absl::TimeFromTimespec(tvdso);
    EXPECT_LE(sys_time, vdso_time)
        << "clock_pair syscall_to_vdso syscall=" << syscall_sample
        << " vdso=" << vdso_sample;
    syscall_sample.Before(page);
    ASSERT_THAT(syscall(__NR_clock_gettime, GetParam(), &tsys),
                SyscallSucceeds());
    syscall_sample.After(page);
    sys_time = absl::TimeFromTimespec(tsys);
    EXPECT_LE(vdso_time, sys_time)
        << "clock_pair vdso_to_syscall vdso=" << vdso_sample
        << " syscall=" << syscall_sample;
  }
}

INSTANTIATE_TEST_SUITE_P(ClockGettime, MonotonicVDSOClockTest,
                         ::testing::Values(CLOCK_MONOTONIC, CLOCK_BOOTTIME),
                         PrintClockId);

}  // namespace

}  // namespace testing
}  // namespace gvisor
