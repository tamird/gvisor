// Copyright 2024 The gVisor Authors.
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

package sys

import (
	"testing"

	"gvisor.dev/gvisor/pkg/errors/linuxerr"
	"gvisor.dev/gvisor/pkg/sentry/contexttest"
	"gvisor.dev/gvisor/pkg/sentry/kernel/auth"
)

func TestFullCPUMask(t *testing.T) {
	for _, test := range []struct {
		want  string
		cores uint
	}{
		{cores: 1, want: "1"},
		{cores: 2, want: "3"},
		{cores: 3, want: "7"},
		{cores: 4, want: "f"},
		{cores: 5, want: "1f"},
		{cores: 32, want: "ffffffff"},
		{cores: 33, want: "1,ffffffff"},
		{cores: 36, want: "f,ffffffff"},
		{cores: 37, want: "1f,ffffffff"},
		{cores: 64, want: "ffffffff,ffffffff"},
		{cores: 65, want: "1,ffffffff,ffffffff"},
	} {
		if got := fullCPUMask(test.cores); got != test.want {
			t.Errorf("fullCPUMask(%d): got %s, want %s", test.cores, got, test.want)
		}
	}
}

func TestOneCPUMask(t *testing.T) {
	for _, test := range []struct {
		want  string
		i     uint
		cores uint
	}{
		{i: 0, cores: 1, want: "1"},
		{i: 0, cores: 4, want: "1"},
		{i: 1, cores: 4, want: "2"},
		{i: 2, cores: 4, want: "4"},
		{i: 3, cores: 4, want: "8"},
		{i: 0, cores: 5, want: "01"},
		{i: 4, cores: 5, want: "10"},
		{i: 0, cores: 32, want: "00000001"},
		{i: 26, cores: 32, want: "04000000"},
		{i: 0, cores: 33, want: "0,00000001"},
		{i: 31, cores: 33, want: "0,80000000"},
		{i: 32, cores: 33, want: "1,00000000"},
		{i: 0, cores: 64, want: "00000000,00000001"},
		{i: 31, cores: 64, want: "00000000,80000000"},
		{i: 32, cores: 64, want: "00000001,00000000"},
		{i: 63, cores: 64, want: "80000000,00000000"},
		{i: 0, cores: 65, want: "0,00000000,00000001"},
		{i: 31, cores: 65, want: "0,00000000,80000000"},
		{i: 32, cores: 65, want: "0,00000001,00000000"},
		{i: 63, cores: 65, want: "0,80000000,00000000"},
		{i: 64, cores: 65, want: "1,00000000,00000000"},
	} {
		if got := oneCPUMask(test.i, test.cores); got != test.want {
			t.Errorf("oneCPUMask(%d, %d): got %s, want %s", test.i, test.cores, got, test.want)
		}
	}
}

func TestCPUMask(t *testing.T) {
	for _, test := range []struct {
		want              string
		first, end, cores uint
	}{
		{first: 0, end: 2, cores: 4, want: "3"},
		{first: 2, end: 4, cores: 4, want: "c"},
		{first: 1, end: 4, cores: 5, want: "0e"},
		{first: 0, end: 43, cores: 86, want: "000000,000007ff,ffffffff"},
		{first: 43, end: 86, cores: 86, want: "3fffff,fffff800,00000000"},
		{first: 32, end: 64, cores: 65, want: "0,ffffffff,00000000"},
	} {
		if got := cpuMask(test.first, test.end, test.cores); got != test.want {
			t.Errorf("cpuMask(%d, %d, %d): got %s, want %s", test.first, test.end, test.cores, got, test.want)
		}
	}
}

func TestIsCPUDirName(t *testing.T) {
	for _, test := range []struct {
		name string
		want bool
	}{
		{"cpu0", true},
		{"cpu12", true},
		{"cpu", false},
		{"cpufreq", false},
		{"cpuidle", false},
		{"cpu1a", false},
		{"online", false},
		{"xcpu0", false},
	} {
		if got := isCPUDirName(test.name); got != test.want {
			t.Errorf("isCPUDirName(%q): got %t, want %t", test.name, got, test.want)
		}
	}
}

// TestCPUDirLookup checks that /sys/devices/system/cpu only creates the cpuN
// directories when one of them is looked up.
func TestCPUDirLookup(t *testing.T) {
	ctx := contexttest.Context(t)
	creds := auth.NewRootCredentials(auth.NewRootUserNamespace())
	const cores = 4
	d := (&filesystem{}).newLazyCPURootDir(ctx, creds, cores)
	defer d.DecRef(ctx)

	if got, want := d.Links(), uint32(2+cores); got != want {
		t.Errorf("link count: got %d, want %d", got, want)
	}
	for _, name := range []string{"online", "possible", "present"} {
		inode, err := d.Lookup(ctx, name)
		if err != nil {
			t.Fatalf("Lookup(%q): %v", name, err)
		}
		inode.DecRef(ctx)
	}
	if _, err := d.Lookup(ctx, "cpufreq"); !linuxerr.Equals(linuxerr.ENOENT, err) {
		t.Errorf("Lookup(cpufreq): got error %v, want ENOENT", err)
	}
	if d.populated.Load() {
		t.Errorf("directory is populated after lookups of non-cpuN names")
	}

	inode, err := d.Lookup(ctx, "cpu3")
	if err != nil {
		t.Fatalf("Lookup(cpu3): %v", err)
	}
	inode.DecRef(ctx)
	if !d.populated.Load() {
		t.Errorf("directory is not populated after lookup of cpu3")
	}
	if _, err := d.Lookup(ctx, "cpu4"); !linuxerr.Equals(linuxerr.ENOENT, err) {
		t.Errorf("Lookup(cpu4): got error %v, want ENOENT", err)
	}
}

func TestCPUListString(t *testing.T) {
	for _, test := range []struct {
		want       string
		first, end uint
	}{
		{first: 0, end: 1, want: "0\n"},
		{first: 3, end: 4, want: "3\n"},
		{first: 0, end: 2, want: "0-1\n"},
		{first: 0, end: 3, want: "0-2\n"},
		{first: 2, end: 4, want: "2-3\n"},
		{first: 0, end: 64, want: "0-63\n"},
		{first: 0, end: 65, want: "0-64\n"},
	} {
		if got := cpuListString(test.first, test.end); got != test.want {
			t.Errorf("cpuListString(%d, %d): got %q, want %q", test.first, test.end, got, test.want)
		}
	}
}
