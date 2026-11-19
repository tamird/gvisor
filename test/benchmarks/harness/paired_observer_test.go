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

package harness

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// releaseDigests covers the whole opaque fileset without naming its members.
func releaseDigests(dir string) (map[string]string, error) {
	result := make(map[string]string)
	var walk func(string) error
	walk = func(dir string) error {
		entries, err := os.ReadDir(dir)
		if err != nil {
			return err
		}
		for _, entry := range entries {
			path := filepath.Join(dir, entry.Name())
			info, err := os.Stat(path)
			if err != nil {
				return err
			}
			if info.IsDir() {
				if err := walk(path); err != nil {
					return err
				}
				continue
			}
			resolved, err := filepath.EvalSymlinks(path)
			if err != nil {
				return err
			}
			digest, err := executableDigest(path)
			if err != nil {
				return err
			}
			result[resolved] = digest
		}
		return nil
	}
	return result, walk(dir)
}

func executableDigest(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return fmt.Sprintf("%x", h.Sum(nil)), nil
}

// ownedReleaseProcesses reports processes still executing declared products.
func ownedReleaseProcesses(products map[string]string) (map[int]string, error) {
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return nil, err
	}
	live := make(map[int]string)
	for _, entry := range entries {
		pid, err := strconv.Atoi(entry.Name())
		if err != nil {
			continue
		}
		exe, err := os.Readlink(filepath.Join("/proc", entry.Name(), "exe"))
		if os.IsNotExist(err) {
			continue
		}
		if err != nil {
			return nil, err
		}
		if _, ok := products[exe]; ok {
			live[pid] = exe
		}
	}
	return live, nil
}

// recordCPUResources reads only the owned worker's processes and control files.
// Raw counters retain the kernel's units and avoid another resource parser.
func recordCPUResources(visit int, trigger string, active map[int]string, products map[string]string) error {
	type process struct {
		Role    string `json:"role"`
		CPUs    string `json:"allowed_cpus"`
		Cgroups string `json:"cgroups"`
	}
	processes := make(map[int]process)
	files := make(map[string]string)
	read := func(path string) error {
		if _, ok := files[path]; ok {
			return nil
		}
		contents, err := os.ReadFile(path)
		if os.IsNotExist(err) {
			return nil
		}
		if err != nil {
			return err
		}
		files[path] = string(contents)
		return nil
	}
	owned, err := ownedReleaseProcesses(products)
	if err != nil {
		return err
	}
	pids := make(map[int]string)
	for pid, role := range active {
		pids[pid] = role
	}
	for pid, exe := range owned {
		role := filepath.Base(exe)
		if cmdline, err := os.ReadFile(fmt.Sprintf("/proc/%d/cmdline", pid)); err == nil {
			role = filepath.Base(strings.SplitN(string(cmdline), "\x00", 2)[0])
		}
		pids[pid] = role
	}
	pids[os.Getpid()] = "paired-worker"
	for pid, role := range pids {
		status, err := os.ReadFile(fmt.Sprintf("/proc/%d/status", pid))
		if os.IsNotExist(err) {
			continue
		}
		if err != nil {
			return err
		}
		membership, err := os.ReadFile(fmt.Sprintf("/proc/%d/cgroup", pid))
		if os.IsNotExist(err) {
			continue
		}
		if err != nil {
			return err
		}
		cpus := ""
		for _, line := range strings.Split(string(status), "\n") {
			if value, ok := strings.CutPrefix(line, "Cpus_allowed_list:"); ok {
				cpus = strings.TrimSpace(value)
			}
		}
		processes[pid] = process{role, cpus, string(membership)}
		for _, line := range strings.Split(strings.TrimSpace(string(membership)), "\n") {
			parts := strings.SplitN(line, ":", 3)
			if len(parts) != 3 {
				return fmt.Errorf("unexpected owned cgroup membership %q", line)
			}
			for _, controller := range strings.Split(parts[1], ",") {
				base := filepath.Join("/sys/fs/cgroup", controller)
				dir := filepath.Join(base, strings.TrimPrefix(parts[2], "/"))
				for dir == base || strings.HasPrefix(dir, base+"/") {
					for _, name := range []string{"cpu.max", "cpu.stat", "cpu.cfs_quota_us", "cpu.cfs_period_us", "cpuset.cpus.effective", "cpuset.effective_cpus", "cpuset.cpus", "memory.max", "memory.current", "memory.events", "memory.limit_in_bytes", "memory.usage_in_bytes", "cpu.pressure", "memory.pressure"} {
						if err := read(filepath.Join(dir, name)); err != nil {
							return err
						}
					}
					if dir == base {
						break
					}
					dir = filepath.Dir(dir)
				}
			}
		}
	}
	for _, path := range []string{"/sys/devices/system/cpu/online", "/proc/meminfo", "/proc/pressure/cpu", "/proc/pressure/memory", "/proc/stat"} {
		if err := read(path); err != nil {
			return err
		}
	}
	return json.NewEncoder(os.Stdout).Encode(struct {
		Visit     int               `json:"resource_visit"`
		Trigger   string            `json:"resource_trigger"`
		Processes map[int]process   `json:"processes"`
		Files     map[string]string `json:"control_files"`
	}{visit, trigger, processes, files})
}
