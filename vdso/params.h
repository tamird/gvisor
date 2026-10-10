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

#ifndef VDSO_PARAMS_H_
#define VDSO_PARAMS_H_

#include <stdint.h>

// struct params defines the layout of the parameter page maintained by the
// kernel (i.e., sentry).
//
// This is similar to the VVAR page maintained by the normal Linux kernel for
// its VDSO, but it has a different layout.
//
// It must be kept in sync with VDSOParamPage in pkg/sentry/kernel/vdso.go.
struct params {
  uint64_t seq_count;

  uint64_t monotonic_ready;
  int64_t monotonic_base_cycles;
  int64_t monotonic_base_ref;
  uint64_t monotonic_frequency;

  uint64_t realtime_ready;
  int64_t realtime_base_cycles;
  int64_t realtime_base_ref;
  uint64_t realtime_frequency;

  uint64_t monotonic_raw_alias;
  uint64_t monotonic_raw_ready;
  int64_t monotonic_raw_base_cycles;
  int64_t monotonic_raw_base_ref;
  uint64_t monotonic_raw_frequency;
};

#endif  // VDSO_PARAMS_H_
