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

package vfs

import (
	"bytes"
	"testing"
	"unsafe"

	"gvisor.dev/gvisor/pkg/abi/linux"
	"gvisor.dev/gvisor/pkg/errors/linuxerr"
	"gvisor.dev/gvisor/pkg/sentry/contexttest"
	"gvisor.dev/gvisor/pkg/sentry/kernel/auth"
	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/waiter"
)

// Host-backed file descriptions queue the waiter before asking the host to
// monitor the FD. A notification can arrive before that request fails.
type falliblePollFD struct {
	testFD
	waiter.Queue
	err       error
	failEvent waiter.EventMask
	last      *waiter.Entry
}

func (fd *falliblePollFD) EpollSource() EpollSource { return fd }

func (fd *falliblePollFD) UpdateMask(e *waiter.Entry, mask waiter.EventMask) error {
	if fd.err != nil {
		fd.Notify(fd.failEvent)
		return fd.err
	}
	return fd.Queue.UpdateMask(e, mask)
}

func (fd *falliblePollFD) EventRegister(e *waiter.Entry) error {
	fd.Queue.EventRegister(e)
	if fd.err != nil {
		fd.Notify(fd.failEvent)
		fd.EventUnregister(e)
	} else {
		fd.last = e
	}
	return fd.err
}

func newEpollTestFD(t *testing.T, fd *falliblePollFD) (*EpollInstance, *FileDescription) {
	t.Helper()
	ctx := contexttest.Context(t)
	var vfs VirtualFilesystem
	if err := vfs.Init(ctx); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { vfs.Release(ctx) })
	epfd, err := vfs.NewEpollInstanceFD(ctx)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { epfd.DecRef(ctx) })
	ep := epfd.Impl().(*EpollInstance)
	vd := vfs.NewAnonVirtualDentry("fallible poll")
	t.Cleanup(func() { vd.DecRef(ctx) })
	file := &fd.fileDescription.vfsfd
	if err := file.Init(fd, linux.O_RDONLY, auth.CredentialsFromContext(ctx), vd.Mount(), vd.Dentry(), &FileDescriptionOptions{}); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { file.DecRef(ctx) })
	return ep, file
}

func TestEpollFailedRegistration(t *testing.T) {
	fd := &falliblePollFD{err: linuxerr.ENOSPC, failEvent: waiter.EventIn}
	ep, file := newEpollTestFD(t, fd)
	const fdnum = 3
	event := linux.EpollEvent{Events: linux.EPOLLIN}
	if got, want := ep.AddInterest(file, fdnum, event), linuxerr.ENOSPC; got != want {
		t.Fatalf("first ADD: got %v, want %v", got, want)
	}
	if got := ep.ReadEvents(nil, 1); len(got) != 0 {
		t.Errorf("events after failed ADD: got %v, want none", got)
	}
	fd.err = nil
	if err := ep.AddInterest(file, fdnum, event); err != nil {
		t.Errorf("retrying ADD: %v", err)
	}
}

func TestEpollFailedModify(t *testing.T) {
	for _, tc := range []struct {
		name    string
		flags   uint32
		consume bool
	}{
		{"pending_edge", linux.EPOLLET, false},
		{"delivered_edge", linux.EPOLLET, true},
		{"disarmed_one_shot", linux.EPOLLONESHOT, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			fd := &falliblePollFD{failEvent: waiter.EventOut}
			ep, file := newEpollTestFD(t, fd)
			const num = 3
			old := linux.EpollEvent{Events: linux.EPOLLIN | tc.flags, Data: [2]int32{1}}
			next := linux.EpollEvent{Events: linux.EPOLLIN | linux.EPOLLOUT | tc.flags, Data: [2]int32{2}}
			read := func(want linux.EpollEvent) {
				t.Helper()
				got := ep.ReadEvents(nil, 1)
				if want.Events == 0 && len(got) == 0 {
					return
				}
				if len(got) != 1 || got[0] != want {
					t.Fatalf("epoll_wait: got %v, want %v", got, want)
				}
			}
			if err := ep.AddInterest(file, num, old); err != nil {
				t.Fatal(err)
			}
			original := fd.last
			in := linux.EpollEvent{Events: linux.EPOLLIN, Data: old.Data}
			if tc.consume {
				read(in)
			}
			fd.err = linuxerr.ENOSPC
			if got := ep.ModifyInterest(file, num, next); got != linuxerr.ENOSPC {
				t.Fatalf("MOD: got %v, want ENOSPC", got)
			}
			if !tc.consume {
				read(in)
			}
			read(linux.EpollEvent{})
			fd.Notify(waiter.EventIn)
			if tc.flags == linux.EPOLLONESHOT {
				read(linux.EpollEvent{})
			} else {
				read(in)
			}

			fd.err = nil
			if err := ep.ModifyInterest(file, num, next); err != nil {
				t.Fatalf("retry MOD: %v", err)
			}
			read(linux.EpollEvent{Events: linux.EPOLLIN | linux.EPOLLOUT, Data: next.Data})
			if fd.last != original {
				t.Fatal("MOD replaced the registered waiter")
			}

			// Changing the mask back must deliver current readiness again.
			if err := ep.ModifyInterest(file, num, old); err != nil {
				t.Fatal(err)
			}
			read(in)
		})
	}
}

func TestEpollRestoreOneShot(t *testing.T) {
	for _, tc := range []struct {
		name    string
		consume bool
	}{
		{"armed", false},
		{"disarmed", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ctx := contexttest.Context(t)
			inner, file := newEpollTestFD(t, &falliblePollFD{})
			fs := inner.vfsfd.VirtualDentry().Mount().Filesystem().VirtualFilesystem()
			outerFD, err := fs.NewEpollInstanceFD(ctx)
			if err != nil {
				t.Fatal(err)
			}
			defer outerFD.DecRef(ctx)
			outer := outerFD.Impl().(*EpollInstance)
			if err := inner.AddInterest(file, 3, linux.EpollEvent{Events: linux.EPOLLIN}); err != nil {
				t.Fatal(err)
			}
			watch := linux.EpollEvent{Events: linux.EPOLLIN | linux.EPOLLONESHOT, Data: [2]int32{17}}
			if err := outer.AddInterest(&inner.vfsfd, 4, watch); err != nil {
				t.Fatal(err)
			}
			if tc.consume {
				got := outer.ReadEvents(nil, 1)
				if len(got) != 1 || got[0] != (linux.EpollEvent{Events: linux.EPOLLIN, Data: watch.Data}) {
					t.Fatalf("consuming one-shot: %v", got)
				}
			}
			// Only the two real epoll descriptors remain in the checkpoint.
			if err := inner.DeleteInterest(file, 3); err != nil {
				t.Fatal(err)
			}
			snapshot := [2]*FileDescription{outerFD, &inner.vfsfd}
			var buf bytes.Buffer
			if _, err := state.Save(ctx, &buf, &snapshot); err != nil {
				t.Fatal(err)
			}
			var restored [2]*FileDescription
			if _, err := state.Load(ctx, &buf, &restored); err != nil {
				t.Fatal(err)
			}
			defer restored[0].VirtualDentry().Mount().Filesystem().VirtualFilesystem().Release(ctx)
			defer restored[1].DecRef(ctx)
			defer restored[0].DecRef(ctx)
			outer = restored[0].Impl().(*EpollInstance)
			// The ready list is not saved; its entries come from afterLoad.
			// Reading the outer epoll would discard the recheck in both cases.
			outer.readyMu.Lock()
			queued := !outer.ready.Empty()
			outer.readyMu.Unlock()
			if want := !tc.consume; queued != want {
				t.Errorf("after restore: recheck queued = %t, want %t", queued, want)
			}
		})
	}
}


func TestEpollWatchSize(t *testing.T) {
	fd := &falliblePollFD{}
	ep, file := newEpollTestFD(t, fd)
	event := linux.EpollEvent{Events: linux.EPOLLIN}
	result := testing.Benchmark(func(b *testing.B) {
		for b.Loop() {
			if err := ep.AddInterest(file, 3, event); err != nil {
				b.Fatal(err)
			}
			if err := ep.DeleteInterest(file, 3); err != nil {
				b.Fatal(err)
			}
		}
	})
	t.Logf("watch struct=%d bytes; ADD+DEL=%d bytes, %d allocations", unsafe.Sizeof(epollInterest{}), result.AllocedBytesPerOp(), result.AllocsPerOp())
}
