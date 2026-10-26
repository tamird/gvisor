// Copyright 2021 The gVisor Authors.
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

package lisafs_test

import (
	"slices"
	"testing"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/context"
	"gvisor.dev/gvisor/pkg/lisafs"
	"gvisor.dev/gvisor/pkg/lisafs/testsuite"
	"gvisor.dev/gvisor/pkg/log"
	"gvisor.dev/gvisor/runsc/config"
	"gvisor.dev/gvisor/runsc/fsgofer"
)

// Note that these are not supposed to be extensive or robust tests. These unit
// tests provide a sanity check that all RPCs at least work in obvious ways.

func init() {
	log.SetLevel(log.Debug)
	if err := fsgofer.OpenProcSelfFD("/proc/self/fd"); err != nil {
		panic(err)
	}
}

// tester implements testsuite.Tester.
type tester struct{}

// NewConnImpl implements testsuite.Tester.NewServer.
func (tester) NewConnImpl(t *testing.T) lisafs.ConnectionImpl {
	return fsgofer.NewConnectionImpl(&fsgofer.Config{HostUDS: config.HostUDSAll})
}

// LinkSupported implements testsuite.Tester.LinkSupported.
func (tester) LinkSupported() bool {
	return true
}

// SetUserGroupIDSupported implements testsuite.Tester.SetUserGroupIDSupported.
func (tester) SetUserGroupIDSupported() bool {
	return true
}

// BindSupported implements testsuite.Tester.BindSupported.
func (tester) BindSupported() bool {
	return true
}

func TestFSGofer(t *testing.T) {
	testsuite.RunAllLocalFSTests(t, tester{})
}

func TestXattrWithoutReadPermission(t *testing.T) {
	dir := t.TempDir()
	if err := unix.Chmod(dir, 0300); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := unix.Chmod(dir, 0700); err != nil {
			t.Errorf("restore directory permissions: %v", err)
		}
	})
	// The connection must fall back to O_PATH; privileged reads would miss the
	// bug even though the directory has no read permission.
	if fd, err := unix.Open(dir, unix.O_RDONLY|unix.O_DIRECTORY, 0); err == nil {
		unix.Close(fd)
		t.Skip("requires directory read permission to be enforced")
	} else if err != unix.EACCES {
		t.Fatalf("open unreadable directory: got %v, want EACCES", err)
	}
	testsuite.RunTest(t, tester{}, "directory", func(ctx context.Context, t *testing.T, _ testsuite.Tester, fd lisafs.ClientFD) {
		const name, value = "user.test", "value"
		if err := fd.SetXattr(ctx, name, value, 0); err != nil {
			t.Fatalf("SetXattr: %v", err)
		}
		if names, err := fd.ListXattr(ctx, 0); err != nil || !slices.Contains(names, name) {
			t.Fatalf("ListXattr: got %v, %v; want %q", names, err, name)
		}
		if _, err := fd.GetXattr(ctx, name, 0); err != unix.EACCES {
			t.Fatalf("GetXattr without read permission: got %v, want EACCES", err)
		}
		// Adding read permission does not change the existing O_PATH descriptor.
		if err := unix.Chmod(dir, 0700); err != nil {
			t.Fatal(err)
		}
		if got, err := fd.GetXattr(ctx, name, 0); err != nil || got != value {
			t.Fatalf("GetXattr: got %q, %v; want %q", got, err, value)
		}
		if err := fd.RemoveXattr(ctx, name); err != nil {
			t.Fatalf("RemoveXattr: %v", err)
		}
		if names, err := fd.ListXattr(ctx, 0); err != nil || slices.Contains(names, name) {
			t.Fatalf("ListXattr after removal: got %v, %v; want no %q", names, err, name)
		}
	}, dir)
}
