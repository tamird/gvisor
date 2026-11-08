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

package linux

import (
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/abi/linux"
	"gvisor.dev/gvisor/pkg/context"
	"gvisor.dev/gvisor/pkg/errors/linuxerr"
	"gvisor.dev/gvisor/pkg/sentry/fsimpl/testutil"
	"gvisor.dev/gvisor/pkg/sentry/fsimpl/tmpfs"
	"gvisor.dev/gvisor/pkg/sentry/kernel"
	"gvisor.dev/gvisor/pkg/sentry/kernel/auth"
	"gvisor.dev/gvisor/pkg/sentry/vfs"
	"gvisor.dev/gvisor/pkg/waiter"
)

type rejectingPollFD struct {
	vfs.FileDescriptionImpl
	unregisters int
}

func (*rejectingPollFD) EventRegister(*waiter.Entry) error { return linuxerr.EIO }
func (fd *rejectingPollFD) EventUnregister(*waiter.Entry)  { fd.unregisters++ }
func (*rejectingPollFD) Release(context.Context)           {}
func (*rejectingPollFD) SupportsLocks() bool               { return false }

func TestPollRejectedRegistration(t *testing.T) {
	k, err := testutil.Boot()
	if err != nil {
		t.Fatal(err)
	}
	ctx := k.SupervisorContext()
	creds := auth.CredentialsFromContext(ctx)
	fs := k.VFS()
	ns, err := fs.NewMountNamespace(ctx, creds, "", tmpfs.Name, &vfs.MountOptions{}, k)
	if err != nil {
		t.Fatal(err)
	}
	s := testutil.NewSystem(ctx, t, fs, ns)
	defer s.Destroy()
	backing, err := fs.OpenAt(ctx, creds, s.PathOpAtRoot("."), &vfs.OpenOptions{Flags: linux.O_RDONLY})
	if err != nil {
		t.Fatal(err)
	}
	defer backing.DecRef(ctx)
	impl := &rejectingPollFD{FileDescriptionImpl: backing.Impl()}
	var fd vfs.FileDescription
	if err := fd.Init(impl, linux.O_RDONLY, creds, backing.Mount(), backing.Dentry(), &vfs.FileDescriptionOptions{}); err != nil {
		t.Fatal(err)
	}
	defer fd.DecRef(ctx)
	tg := k.NewThreadGroup(k.RootPIDNamespace(), kernel.NewSignalHandlers(), linux.SIGCHLD, k.GlobalInit().Limits())
	task, err := testutil.CreateTask(ctx, "poll", tg, ns, s.Root, s.Root)
	if err != nil {
		t.Fatal(err)
	}
	n, err := task.NewFDFrom(0, &fd, kernel.FDFlags{})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { task.FDTable().Remove(task, n).DecRef(task) }()
	refs := fd.ReadRefs()
	_, _, err = pollBlock(task, []linux.PollFD{{FD: n, Events: linux.POLLIN}}, time.Second)
	if !linuxerr.Equals(linuxerr.EIO, err) {
		t.Errorf("pollBlock returned %v, want EIO", err)
	}
	if impl.unregisters != 0 {
		t.Errorf("EventUnregister called %d times after rejected registration, want 0", impl.unregisters)
	}
	if got := fd.ReadRefs(); got != refs {
		t.Errorf("file reference count after pollBlock = %d, want %d", got, refs)
	}
}
