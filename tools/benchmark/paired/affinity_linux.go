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

package main

import "golang.org/x/sys/unix"

func getAffinity(pid int) ([]int, error) {
	var set unix.CPUSet
	if err := unix.SchedGetaffinity(pid, &set); err != nil {
		return nil, err
	}
	cpus := make([]int, 0, set.Count())
	for cpu, remaining := 0, set.Count(); remaining > 0; cpu++ {
		if set.IsSet(cpu) {
			cpus = append(cpus, cpu)
			remaining--
		}
	}
	return cpus, nil
}

func setAffinity(cpu int) error {
	var set unix.CPUSet
	set.Set(cpu)
	return unix.SchedSetaffinity(0, &set)
}
