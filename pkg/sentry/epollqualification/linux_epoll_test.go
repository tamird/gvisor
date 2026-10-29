package qualification

import (
	"golang.org/x/sys/unix"
	"testing"
)

func TestLinuxEpollModify(t *testing.T) {
	efd, err := unix.Eventfd(1, unix.EFD_NONBLOCK)
	if err != nil {
		t.Fatal(err)
	}
	defer unix.Close(efd)
	ep, err := unix.EpollCreate1(0)
	if err != nil {
		t.Fatal(err)
	}
	defer unix.Close(ep)
	var slot [1]unix.EpollEvent
	poll := func(want int32) {
		t.Helper()
		n, err := unix.EpollWait(ep, slot[:], 0)
		if err != nil {
			t.Fatal(err)
		}
		if want == 0 {
			if n != 0 {
				t.Fatalf("got %v, want no event", slot[0])
			}
			return
		}
		if n != 1 || slot[0].Events != unix.EPOLLIN || slot[0].Fd != want {
			t.Fatalf("got (%d,%v), want IN data %d", n, slot[0], want)
		}
	}
	mod := func(data int32, extra uint32) error {
		return unix.EpollCtl(ep, unix.EPOLL_CTL_MOD, efd, &unix.EpollEvent{Events: unix.EPOLLIN | unix.EPOLLONESHOT | extra, Fd: data})
	}
	if err := unix.EpollCtl(ep, unix.EPOLL_CTL_ADD, efd, &unix.EpollEvent{Events: unix.EPOLLIN | unix.EPOLLONESHOT, Fd: 1}); err != nil {
		t.Fatal(err)
	}
	if err := mod(2, unix.EPOLLEXCLUSIVE); err != unix.EINVAL {
		t.Fatalf("invalid pending MOD: %v", err)
	}
	poll(1)
	if err := mod(2, unix.EPOLLEXCLUSIVE); err != unix.EINVAL {
		t.Fatalf("invalid disarmed MOD: %v", err)
	}
	poll(0)
	if err := mod(3, 0); err != nil {
		t.Fatal(err)
	}
	poll(3)
	poll(0)
}
