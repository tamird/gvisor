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

package proc

import (
	"testing"

	"gvisor.dev/gvisor/pkg/abi/linux"
	"gvisor.dev/gvisor/pkg/sentry/fsimpl/overlay"
	"gvisor.dev/gvisor/pkg/sentry/kernel"
	"gvisor.dev/gvisor/pkg/sentry/vfs"
	"gvisor.dev/gvisor/pkg/waiter"
)

func TestOverlayPollUnregisterAfterCopyUp(t *testing.T) {
	s := setup(t)
	k := kernel.KernelFromContext(s.Ctx)
	defer s.Destroy()
	s.VFS.MustRegisterFilesystemType(overlay.Name, overlay.FilesystemType{}, &vfs.RegisterFilesystemTypeOptions{AllowUserMount: true})
	if err := s.VFS.MkdirAt(s.Ctx, s.Creds, s.PathOpAtRoot("/upper"), &vfs.MkdirOptions{Mode: 0755}); err != nil {
		t.Fatal(err)
	}
	upper := s.GetDentryOrDie(s.PathOpAtRoot("/upper"))
	defer upper.DecRef(s.Ctx)
	lower := s.GetDentryOrDie(s.PathOpAtRoot("/proc/sys/kernel"))
	defer lower.DecRef(s.Ctx)
	ns, err := s.VFS.NewMountNamespace(s.Ctx, s.Creds, "", overlay.Name, &vfs.MountOptions{
		GetFilesystemOptions: vfs.GetFilesystemOptions{InternalData: overlay.FilesystemOptions{UpperRoot: upper, LowerRoots: []vfs.VirtualDentry{lower}}},
	}, k)
	if err != nil {
		t.Fatal(err)
	}
	defer ns.DecRef(s.Ctx)
	root := ns.Root(s.Ctx)
	defer root.DecRef(s.Ctx)
	path := s.PathOpAtRoot("hostname")
	path.Root = root
	path.Start = root
	fd, err := s.VFS.OpenAt(s.Ctx, s.Creds, path, &vfs.OpenOptions{Flags: linux.O_RDONLY})
	if err != nil {
		t.Fatal(err)
	}
	defer fd.DecRef(s.Ctx)
	for _, tc := range []struct {
		name   string
		copyUp bool
	}{{"same_lower", false}, {"copied_to_tmpfs", true}} {
		t.Run(tc.name, func(t *testing.T) {
			epfd, err := s.VFS.NewEpollInstanceFD(s.Ctx)
			if err != nil {
				t.Fatal(err)
			}
			defer epfd.DecRef(s.Ctx)
			ep := epfd.Impl().(*vfs.EpollInstance)
			w, ch := waiter.NewChannelEntry(waiter.ReadableEvents)
			if err := epfd.EventRegister(&w); err != nil {
				t.Fatal(err)
			}
			defer epfd.EventUnregister(&w)
			if err := ep.AddInterest(fd, 3, linux.EpollEvent{Events: linux.EPOLLPRI | linux.EPOLLET}); err != nil {
				t.Fatalf("register /proc/sys/kernel/hostname: %v", err)
			}
			k.RootUTSNamespace().SetHostName(s.Ctx, tc.name+"-before")
			select {
			case <-ch:
			default:
				t.Fatal("live hostname interest did not wake epoll")
			}
			if got := ep.ReadEvents(nil, 1); len(got) != 1 || got[0].Events&linux.EPOLLPRI == 0 {
				t.Fatalf("hostname change: got %v, want one EPOLLPRI event", got)
			}
			if tc.copyUp {
				if err := fd.SetStat(s.Ctx, vfs.SetStatOptions{Stat: linux.Statx{Mask: linux.STATX_MODE, Mode: 0600}}); err != nil {
					t.Fatalf("copy hostname up through chmod: %v", err)
				}
				source := fd.EpollSource()
				if source.Backing != nil {
					source.Backing.DecRef(s.Ctx)
				}
				if source.Owner != nil {
					t.Fatal("file still uses the epollable proc lower after copy-up")
				}
			}
			// Only the original proc file can report priority events. The
			// tmpfs upper is now the file chosen by new operations.
			k.RootUTSNamespace().SetHostName(s.Ctx, tc.name+"-while-registered")
			select {
			case <-ch:
			default:
				t.Fatal("hostname changed but the installed watch did not wake")
			}
			if got := ep.ReadEvents(nil, 1); len(got) != 1 || got[0].Events != linux.EPOLLPRI {
				t.Fatalf("hostname changed on registered backing: got %v, want EPOLLPRI", got)
			}
			if err := ep.DeleteInterest(fd, 3); err != nil {
				t.Fatalf("delete hostname interest: %v", err)
			}
			select {
			case <-ch:
				t.Fatal("unexpected epoll wake before changing hostname again")
			default:
			}
			k.RootUTSNamespace().SetHostName(s.Ctx, tc.name+"-after")
			select {
			case <-ch:
				t.Error("hostname change woke epoll after its interest was deleted")
			default:
			}
			if got := ep.ReadEvents(nil, 1); len(got) != 0 {
				t.Errorf("events after deletion: got %v, want none", got)
			}
		})
	}
}
