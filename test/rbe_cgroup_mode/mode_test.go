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

package mode_test

import (
	"encoding/json"
	"os"
	"runtime"
	"strings"
	"testing"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/runsc/cgroup"
)

func TestWorkerCgroupMode(t *testing.T) {
	setting, ok := os.LookupEnv("CGROUPV2")
	if !ok || (setting != "true" && setting != "false") {
		t.Fatalf("unexpected CGROUPV2 setting %q (present=%v)", setting, ok)
	}
	var fs unix.Statfs_t
	if err := unix.Statfs("/sys/fs/cgroup", &fs); err != nil {
		t.Fatal(err)
	}
	self, err := os.ReadFile("/proc/self/cgroup")
	if err != nil {
		t.Fatal(err)
	}
	release, err := os.ReadFile("/proc/sys/kernel/osrelease")
	if err != nil {
		t.Fatal(err)
	}
	var hierarchyIDs []string
	for _, line := range strings.Split(strings.TrimSpace(string(self)), "\n") {
		parts := strings.SplitN(line, ":", 3)
		if len(parts) != 3 {
			t.Fatalf("malformed cgroup membership")
		}
		hierarchyIDs = append(hierarchyIDs, parts[0])
	}
	observed := cgroup.IsOnlyV2()
	if observed != (fs.Type == unix.CGROUP2_SUPER_MAGIC) {
		t.Fatalf("IsOnlyV2=%v with filesystem type %#x", observed, fs.Type)
	}
	data, err := json.Marshal(map[string]any{
		"arch": runtime.GOARCH, "release": strings.TrimSpace(string(release)),
		"requested_isolation": os.Getenv("TEST_ISOLATION"), "setting": setting,
		"fs_magic": fs.Type, "only_v2": observed, "hierarchy_ids": hierarchyIDs, "uid": os.Getuid(),
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("CGROUP_MODE %s", data)
}
