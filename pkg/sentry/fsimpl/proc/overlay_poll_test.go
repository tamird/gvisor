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
	"gvisor.dev/gvisor/pkg/errors/linuxerr"
	"gvisor.dev/gvisor/pkg/sentry/fsimpl/overlay"
	"gvisor.dev/gvisor/pkg/sentry/kernel"
	"gvisor.dev/gvisor/pkg/sentry/vfs"
	"gvisor.dev/gvisor/pkg/usermem"
)

func TestOverlayEpollAdmission(t *testing.T) {
	s := setup(t)
	k := kernel.KernelFromContext(s.Ctx)
	defer s.Destroy()
	s.VFS.MustRegisterFilesystemType(overlay.Name, overlay.FilesystemType{}, &vfs.RegisterFilesystemTypeOptions{AllowUserMount: true})
	if err := s.VFS.MkdirAt(s.Ctx, s.Creds, s.PathOpAtRoot("/upper"), &vfs.MkdirOptions{Mode: 0755}); err != nil {
		t.Fatal(err)
	}
	if err := s.VFS.MknodAt(s.Ctx, s.Creds, s.PathOpAtRoot("/upper/fifo"), &vfs.MknodOptions{Mode: linux.S_IFIFO | 0600}); err != nil {
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
	epfd, err := s.VFS.NewEpollInstanceFD(s.Ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer epfd.DecRef(s.Ctx)
	ep := epfd.Impl().(*vfs.EpollInstance)
	open := func(t *testing.T, name string, flags uint32, onOverlay bool) *vfs.FileDescription {
		t.Helper()
		path := s.PathOpAtRoot(name)
		if onOverlay {
			path.Root, path.Start = root, root
		}
		fd, err := s.VFS.OpenAt(s.Ctx, s.Creds, path, &vfs.OpenOptions{Flags: flags})
		if err != nil {
			t.Fatal(err)
		}
		return fd
	}
	t.Run("direct_proc", func(t *testing.T) {
		fd := open(t, "/proc/sys/kernel/hostname", linux.O_RDONLY, false)
		defer fd.DecRef(s.Ctx)
		if err := ep.AddInterest(fd, 3, linux.EpollEvent{Events: linux.EPOLLPRI | linux.EPOLLET}); err != nil {
			t.Fatal(err)
		}
		k.RootUTSNamespace().SetHostName(s.Ctx, "changed")
		if got := ep.ReadEvents(nil, 1); len(got) != 1 || got[0].Events != linux.EPOLLPRI {
			t.Fatalf("direct proc hostname event: got %v, want EPOLLPRI", got)
		}
	})
	t.Run("overlay_regular", func(t *testing.T) {
		fd := open(t, "hostname", linux.O_RDONLY, true)
		defer fd.DecRef(s.Ctx)
		want := linuxerr.EPERM
		if got := ep.AddInterest(fd, 3, linux.EpollEvent{Events: linux.EPOLLPRI}); got != want {
			t.Fatalf("ADD before copy-up: got %v, want %v", got, want)
		}
		if err := fd.SetStat(s.Ctx, vfs.SetStatOptions{Stat: linux.Statx{Mask: linux.STATX_MODE, Mode: 0600}}); err != nil {
			t.Fatalf("copy up regular file: %v", err)
		}
		if got := ep.AddInterest(fd, 3, linux.EpollEvent{Events: linux.EPOLLPRI}); got != want {
			t.Fatalf("ADD after copy-up: got %v, want %v", got, want)
		}
	})
	t.Run("overlay_fifo", func(t *testing.T) {
		fd := open(t, "fifo", linux.O_RDWR|linux.O_NONBLOCK, true)
		defer fd.DecRef(s.Ctx)
		if err := ep.AddInterest(fd, 3, linux.EpollEvent{Events: linux.EPOLLIN}); err != nil {
			t.Fatal(err)
		}
		if got := ep.ReadEvents(nil, 1); len(got) != 0 {
			t.Fatalf("FIFO ready before write: %v", got)
		}
		if n, err := fd.Write(s.Ctx, usermem.BytesIOSequence([]byte{1}), vfs.WriteOptions{}); n != 1 || err != nil {
			t.Fatalf("FIFO write: got (%d, %v), want (1, nil)", n, err)
		}
		if got := ep.ReadEvents(nil, 1); len(got) != 1 || got[0].Events != linux.EPOLLIN {
			t.Fatalf("overlay FIFO: got %v, want EPOLLIN", got)
		}
	})
}
