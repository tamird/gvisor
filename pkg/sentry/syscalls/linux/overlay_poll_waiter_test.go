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

	"gvisor.dev/gvisor/pkg/abi/linux"
	"gvisor.dev/gvisor/pkg/sentry/fsimpl/overlay"
	"gvisor.dev/gvisor/pkg/sentry/fsimpl/proc"
	"gvisor.dev/gvisor/pkg/sentry/fsimpl/testutil"
	"gvisor.dev/gvisor/pkg/sentry/fsimpl/tmpfs"
	"gvisor.dev/gvisor/pkg/sentry/kernel"
	"gvisor.dev/gvisor/pkg/sentry/kernel/auth"
	"gvisor.dev/gvisor/pkg/sentry/vfs"
	"gvisor.dev/gvisor/pkg/waiter"
)

// This fixture runs the registration and cleanup functions called by pollBlock.
// A second poll stays registered on the same hostname notifier throughout.
func TestPollWaiterAfterOverlayCopyUp(t *testing.T) {
	k, err := testutil.Boot()
	if err != nil {
		t.Fatal(err)
	}
	ctx := k.SupervisorContext()
	creds := auth.CredentialsFromContext(ctx)
	fs := k.VFS()
	fs.MustRegisterFilesystemType(proc.Name, &proc.FilesystemType{}, &vfs.RegisterFilesystemTypeOptions{AllowUserMount: true})
	fs.MustRegisterFilesystemType(overlay.Name, overlay.FilesystemType{}, &vfs.RegisterFilesystemTypeOptions{AllowUserMount: true})
	ns, err := fs.NewMountNamespace(ctx, creds, "", tmpfs.Name, &vfs.MountOptions{}, k)
	if err != nil {
		t.Fatal(err)
	}
	s := testutil.NewSystem(ctx, t, fs, ns)
	defer s.Destroy()
	for _, path := range []string{"/proc", "/upper"} {
		if err := fs.MkdirAt(ctx, creds, s.PathOpAtRoot(path), &vfs.MkdirOptions{Mode: 0755}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := fs.MountAt(ctx, creds, "", s.PathOpAtRoot("/proc"), proc.Name, &vfs.MountOptions{GetFilesystemOptions: vfs.GetFilesystemOptions{InternalData: &proc.InternalData{}}}); err != nil {
		t.Fatal(err)
	}
	lower := s.GetDentryOrDie(s.PathOpAtRoot("/proc/sys/kernel"))
	defer lower.DecRef(ctx)
	upper := s.GetDentryOrDie(s.PathOpAtRoot("/upper"))
	defer upper.DecRef(ctx)
	ol, err := fs.NewMountNamespace(ctx, creds, "", overlay.Name, &vfs.MountOptions{GetFilesystemOptions: vfs.GetFilesystemOptions{InternalData: overlay.FilesystemOptions{UpperRoot: upper, LowerRoots: []vfs.VirtualDentry{lower}}}}, k)
	if err != nil {
		t.Fatal(err)
	}
	defer ol.DecRef(ctx)
	root := ol.Root(ctx)
	defer root.DecRef(ctx)
	path := s.PathOpAtRoot("hostname")
	path.Root, path.Start = root, root
	ofd, err := fs.OpenAt(ctx, creds, path, &vfs.OpenOptions{Flags: linux.O_RDONLY})
	if err != nil {
		t.Fatal(err)
	}
	defer ofd.DecRef(ctx)
	peer, err := fs.OpenAt(ctx, creds, s.PathOpAtRoot("/proc/sys/kernel/hostname"), &vfs.OpenOptions{Flags: linux.O_RDONLY})
	if err != nil {
		t.Fatal(err)
	}
	defer peer.DecRef(ctx)
	tg := k.NewThreadGroup(k.RootPIDNamespace(), kernel.NewSignalHandlers(), linux.SIGCHLD, k.GlobalInit().Limits())
	task, err := testutil.CreateTask(ctx, "overlay-poll", tg, s.MntNs, s.Root, s.Root)
	if err != nil {
		t.Fatal(err)
	}
	register := func(fd *vfs.FileDescription) ([]pollState, chan struct{}) {
		n, err := task.NewFDFrom(0, fd, kernel.FDFlags{})
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { task.FDTable().Remove(task, n).DecRef(task) })
		state := make([]pollState, 1)
		ch := make(chan struct{}, 1)
		pfd := linux.PollFD{FD: n, Events: linux.POLLPRI | linux.POLLERR | linux.POLLHUP}
		if err := initReadiness(task, &pfd, &state[0], ch); err != nil {
			t.Fatal(err)
		}
		return state, ch
	}
	peerState, peerCh := register(peer)
	defer releaseState(task, peerState)
	for _, copyUp := range []bool{false, true} {
		state, ch := register(ofd)
		if copyUp {
			if err := ofd.SetStat(ctx, vfs.SetStatOptions{Stat: linux.Statx{Mask: linux.STATX_MODE, Mode: 0600}}); err != nil {
				t.Fatal(err)
			}
			// Force the open overlay description to adopt its copied-up backing.
			ofd.Readiness(waiter.EventPri)
		}
		releaseState(task, state)
		k.HostNamePoller.Notify()
		select {
		case <-peerCh:
		default:
			t.Fatal("active direct-proc poll failed to wake")
		}
		select {
		case <-ch:
			t.Errorf("released poll waiter received a hostname notification (copyUp=%t)", copyUp)
		default:
			t.Logf("released waiter stayed silent and active peer woke (copyUp=%t)", copyUp)
		}
	}
}
