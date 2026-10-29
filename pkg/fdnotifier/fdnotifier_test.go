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

//go:build linux
// +build linux

// Package fdnotifier contains an adapter that translates IO events (e.g., a
// file became readable/writable) from native FDs to the notifications in the
// waiter package. It uses epoll in edge-triggered mode to receive notifications
// for registered FDs.
package fdnotifier_test

import (
	"errors"
	"testing"
	"time"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/fdnotifier"
	"gvisor.dev/gvisor/pkg/waiter"
)

func TestRejectedMaskKeepsHostSubscription(t *testing.T) {
	var pipe [2]int
	if err := unix.Pipe2(pipe[:], unix.O_CLOEXEC); err != nil {
		t.Fatal(err)
	}
	defer unix.Close(pipe[0])
	defer unix.Close(pipe[1])

	fd := int32(pipe[0])
	var q waiter.Queue
	entry, ch := waiter.NewChannelEntry(waiter.EventIn)
	q.EventRegister(&entry)
	defer q.EventUnregister(&entry)
	if err := fdnotifier.AddFD(fd, &q); err != nil {
		t.Fatal(err)
	}
	defer fdnotifier.RemoveFD(fd)

	// Linux rejects EPOLLEXCLUSIVE in EPOLL_CTL_MOD without disturbing the
	// earlier subscription. The software mask must also remain readable.
	if got, want := fdnotifier.UpdateMask(fd, &q, &entry, waiter.EventOut|unix.EPOLLEXCLUSIVE), error(unix.EINVAL); !errors.Is(got, want) {
		t.Fatalf("UpdateMask: got %v, want %v", got, want)
	}
	if got, want := entry.Mask(), waiter.EventIn; got != want {
		t.Fatalf("entry mask: got %v, want %v", got, want)
	}
	if _, err := unix.Write(pipe[1], []byte{1}); err != nil {
		t.Fatal(err)
	}
	select {
	case <-ch:
	case <-time.After(5 * time.Second):
		t.Fatal("no host readiness notification after rejected update")
	}
}
