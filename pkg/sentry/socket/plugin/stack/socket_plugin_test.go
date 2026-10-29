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

//go:build network_plugins

package stack

import (
	"testing"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/waiter"
)

func TestRejectedMaskUpdate(t *testing.T) {
	epfd, err := unix.EpollCreate1(unix.EPOLL_CLOEXEC)
	if err != nil {
		t.Fatal(err)
	}
	defer unix.Close(epfd)
	fds, err := unix.Socketpair(unix.AF_UNIX, unix.SOCK_STREAM|unix.SOCK_CLOEXEC, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer unix.Close(fds[0])
	defer unix.Close(fds[1])

	// TLDK routes ordinary file descriptors to Linux's epoll_ctl.
	s := socketOperations{fd: uint32(fds[0])}
	s.eventInfo.Wq = &waiter.Queue{}
	n := &Notifier{epFD: int32(epfd)}
	prev := stack
	stack = &Stack{notifier: n}
	defer func() { stack = prev }()

	const old = waiter.EventIn
	e, _ := waiter.NewChannelEntry(old)
	if err := s.EventRegister(&e); err != nil {
		t.Fatal(err)
	}
	defer s.EventUnregister(&e)
	s.eventInfo.Ready.Store(uint64(old))
	// Linux rejects EPOLLEXCLUSIVE in MOD, even on an existing watch.
	if got, want := s.UpdateMask(&e, unix.EPOLLEXCLUSIVE|waiter.EventOut), unix.EINVAL; got != want {
		t.Fatalf("MOD returned %v, want %v", got, want)
	}
	if got := e.Mask(); got != old {
		t.Errorf("waiter mask = %#x, want %#x", got, old)
	}
	if got := s.eventInfo.Mask; got != old {
		t.Errorf("plugin mask = %#x, want %#x", got, old)
	}
	if got := s.eventInfo.Ready.Load(); got != uint64(old) {
		t.Errorf("cached readiness = %#x, want %#x", got, old)
	}
	// This idle socket is writable, but the original subscription covers
	// only reads. It should wake only after the peer sends a byte.
	var events [1]unix.EpollEvent
	if n, err := unix.EpollWait(epfd, events[:], 0); err != nil || n != 0 {
		t.Fatalf("idle epoll_wait = %d, %v, want 0, nil", n, err)
	}
	if _, err := unix.Write(fds[1], []byte{'x'}); err != nil {
		t.Fatal(err)
	}
	if n, err := unix.EpollWait(epfd, events[:], 0); err != nil || n != 1 || events[0].Events != unix.EPOLLIN {
		t.Fatalf("epoll_wait after write = %d, %v, events %#x, want 1, nil, IN", n, err, events[0].Events)
	}
}
