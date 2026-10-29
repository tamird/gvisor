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

package transport

import (
	"errors"
	"testing"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/context"
	"gvisor.dev/gvisor/pkg/syserr"
	"gvisor.dev/gvisor/pkg/waiter"
)

// TestSCMSenderBeforeSaveIdempotent verifies that calling beforeSave
// on an SCMSender whose host file descriptor is already closed (fd < 0)
// is a safe no-op and does not panic in fdnotifier.RemoveFD.
func TestSCMSenderBeforeSaveIdempotent(t *testing.T) {
	fds, err := unix.Socketpair(unix.AF_UNIX, unix.SOCK_STREAM, 0)
	if err != nil {
		t.Fatalf("Socketpair failed: %v", err)
	}
	defer unix.Close(fds[1])

	q := &waiter.Queue{}
	ep, sysErr := NewSCMSender(fds[0], q, "test-addr")
	if sysErr != nil {
		unix.Close(fds[0])
		t.Fatalf("NewSCMSender failed: %v", sysErr)
	}

	// First beforeSave cleans up the host FD and sets ep.fd = -1.
	ep.beforeSave()
	if ep.fd != -1 {
		t.Fatalf("after beforeSave, ep.fd = %d, want -1", ep.fd)
	}

	// Second beforeSave must be idempotent and must not panic on fdnotifier.RemoveFD(-1).
	ep.beforeSave()
}

// TestHostSenderClosedHostReadiness verifies that HostReadiness reports
// hangup and readable events when the host socket is closed (fd < 0) so that pollers
// are notified of EOF / disconnection.
func TestHostSenderClosedHostReadiness(t *testing.T) {
	c := &HostSender{fd: -1}

	mask := waiter.ReadableEvents | waiter.EventHUp | waiter.EventRdHUp | waiter.WritableEvents
	got := c.HostReadiness(mask)
	want := (waiter.ReadableEvents | waiter.WritableEvents | waiter.EventHUp | waiter.EventRdHUp) & mask
	if got != want {
		t.Fatalf("HostReadiness(%v) when fd < 0 = %v, want %v", mask, got, want)
	}

	// A severed socket reports writable.
	if gotOut := c.HostReadiness(waiter.EventOut); gotOut != waiter.EventOut {
		t.Errorf("HostReadiness(EventOut) when fd < 0 = %v, want %v", gotOut, waiter.EventOut)
	}

	// Querying EventHUp explicitly must return EventHUp.
	if gotHup := c.HostReadiness(waiter.EventHUp); gotHup != waiter.EventHUp {
		t.Errorf("HostReadiness(EventHUp) when fd < 0 = %v, want %v", gotHup, waiter.EventHUp)
	}

	// Querying EventIn explicitly must return EventIn.
	if gotIn := c.HostReadiness(waiter.EventIn); gotIn != waiter.EventIn {
		t.Errorf("HostReadiness(EventIn) when fd < 0 = %v, want %v", gotIn, waiter.EventIn)
	}
}

// TestConnectionedEndpointClosedHostReadiness verifies that connectionedEndpoint.Readiness
// properly propagates hangup and readable events when backed by a closed HostSender.
func TestConnectionedEndpointClosedHostReadiness(t *testing.T) {
	c := &HostSender{fd: -1}
	ep := &connectionedEndpoint{
		baseEndpoint: baseEndpoint{
			receiver: c,
			peer:     c,
		},
	}

	mask := waiter.EventIn | waiter.EventHUp | waiter.EventRdHUp | waiter.EventOut
	got := ep.Readiness(mask)
	want := (waiter.ReadableEvents | waiter.WritableEvents | waiter.EventHUp | waiter.EventRdHUp) & mask
	if got != want {
		t.Fatalf("Readiness(%v) for closed host endpoint = %v, want %v", mask, got, want)
	}
}

// TestSeveredRecvSend verifies that an SCMSender whose host fd was severed by
// beforeSave reports EOF on receive and EPIPE on send, never EBADF: the
// application's fd is still valid even though the sentry's host fd is gone.
func TestSeveredRecvSend(t *testing.T) {
	fds, err := unix.Socketpair(unix.AF_UNIX, unix.SOCK_STREAM, 0)
	if err != nil {
		t.Fatalf("Socketpair failed: %v", err)
	}
	defer unix.Close(fds[1])

	ep, serr := NewSCMSender(fds[0], &waiter.Queue{}, "test-addr")
	if serr != nil {
		unix.Close(fds[0])
		t.Fatalf("NewSCMSender failed: %v", serr)
	}

	ep.beforeSave()
	if ep.fd != -1 {
		t.Fatalf("after beforeSave, ep.fd = %d, want -1", ep.fd)
	}

	buf := [][]byte{make([]byte, 8)}
	if _, _, serr := ep.Recv(nil, buf, RecvArgs{}); serr != syserr.ErrClosedForReceive {
		t.Errorf("Recv on severed endpoint returned %v, want %v", serr, syserr.ErrClosedForReceive)
	}
	if _, _, serr := ep.Send(nil, [][]byte{{1}}, ControlMessages{}, Address{}); serr != syserr.ErrClosedForSend {
		t.Errorf("Send on severed endpoint returned %v, want %v", serr, syserr.ErrClosedForSend)
	}
}

// A failed host poll must not leave the entry on the Unix endpoint's queue.
func TestFailedHostWaiterRegistration(t *testing.T) {
	fds, err := unix.Socketpair(unix.AF_UNIX, unix.SOCK_STREAM, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer unix.Close(fds[1])
	endpoint := baseEndpoint{WaitQueue: &waiter.Queue{}}
	peer, sysErr := NewSCMSender(fds[0], endpoint.WaitQueue, "")
	if sysErr != nil {
		unix.Close(fds[0])
		t.Fatal(sysErr)
	}
	defer peer.Release(context.Background()) // sender
	defer peer.Release(context.Background()) // receiver
	endpoint.peer = peer

	replaceWithUnpollableFile(t, fds[0])
	entry, ch := waiter.NewChannelEntry(waiter.EventIn)
	if got, want := endpoint.EventRegister(&entry), error(unix.EPERM); !errors.Is(got, want) {
		t.Fatalf("EventRegister: got %v, want %v", got, want)
	}
	endpoint.WaitQueue.Notify(waiter.EventIn)
	select {
	case <-ch:
		t.Fatal("failed registration received a notification")
	default:
	}
}

func replaceWithUnpollableFile(t *testing.T, fd int) {
	t.Helper()
	null, err := unix.Open("/dev/null", unix.O_RDONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer unix.Close(null)
	if err := unix.Dup3(null, fd, unix.O_CLOEXEC); err != nil {
		t.Fatal(err)
	}
}

// testListener is a real host listener used as a BoundSocketFD without a gofer.
type testListener int32

func (fd testListener) NotificationFD() int32 { return int32(fd) }
func (fd testListener) Close(context.Context) { unix.Close(int(fd)) }
func (fd testListener) Listen(_ context.Context, backlog int32) error {
	return unix.Listen(int(fd), int(backlog))
}
func (fd testListener) Accept(context.Context) (int, error) {
	next, _, err := unix.Accept4(int(fd), unix.SOCK_NONBLOCK|unix.SOCK_CLOEXEC)
	return next, err
}

func newTestListener(t *testing.T) testListener {
	t.Helper()
	fd, err := unix.Socket(unix.AF_UNIX, unix.SOCK_STREAM|unix.SOCK_NONBLOCK|unix.SOCK_CLOEXEC, 0)
	if err != nil {
		t.Fatal(err)
	}
	if err := unix.Bind(fd, &unix.SockaddrUnix{}); err != nil {
		unix.Close(fd)
		t.Fatal(err)
	}
	if err := unix.Listen(fd, 1); err != nil {
		unix.Close(fd)
		t.Fatal(err)
	}
	return testListener(fd)
}

func TestUnpollableBoundListener(t *testing.T) {
	ctx := context.Background()
	t.Run("register after bind", func(t *testing.T) {
		ep := &connectionedEndpoint{baseEndpoint: baseEndpoint{WaitQueue: &waiter.Queue{}}}
		fd := newTestListener(t)
		if err := ep.SetBoundSocketFD(ctx, fd); err != nil {
			t.Fatal(err)
		}
		defer ep.ResetBoundSocketFD(ctx)
		replaceWithUnpollableFile(t, int(fd))
		entry, _ := waiter.NewChannelEntry(waiter.EventIn)
		if got, want := ep.EventRegister(&entry), error(unix.EPERM); !errors.Is(got, want) {
			t.Fatalf("EventRegister: got %v, want %v", got, want)
		}
		if !ep.WaitQueue.IsEmpty() {
			t.Fatal("failed registration left the entry on the queue")
		}
	})
	t.Run("bind after register", func(t *testing.T) {
		ep := &connectionedEndpoint{baseEndpoint: baseEndpoint{WaitQueue: &waiter.Queue{}}}
		entry, _ := waiter.NewChannelEntry(waiter.EventIn)
		if err := ep.EventRegister(&entry); err != nil {
			t.Fatal(err)
		}
		defer ep.EventUnregister(&entry)
		fd := newTestListener(t)
		replaceWithUnpollableFile(t, int(fd))
		if got, want := ep.SetBoundSocketFD(ctx, fd), error(unix.EPERM); !errors.Is(got, want) {
			ep.ResetBoundSocketFD(ctx)
			t.Fatalf("SetBoundSocketFD: got %v, want %v", got, want)
		}
	})
}

func TestSCMSenderRejectsInvalidHostSubscription(t *testing.T) {
	fds, err := unix.Socketpair(unix.AF_UNIX, unix.SOCK_STREAM, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer unix.Close(fds[0])
	defer unix.Close(fds[1])

	// Linux rejects EPOLLEXCLUSIVE combined with EPOLLONESHOT.
	var q waiter.Queue
	entry, _ := waiter.NewChannelEntry(unix.EPOLLEXCLUSIVE | unix.EPOLLONESHOT)
	q.EventRegister(&entry)
	defer q.EventUnregister(&entry)
	if _, err := NewSCMSender(fds[0], &q, ""); err != syserr.ErrInvalidArgument {
		t.Fatalf("NewSCMSender: got %v, want EINVAL", err)
	}
	if _, err := unix.Write(fds[0], []byte("x")); err != nil {
		t.Fatalf("write on caller-owned socket after failed registration: %v", err)
	}
}
