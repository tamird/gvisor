package rawfile

import (
	"testing"

	"golang.org/x/sys/unix"
)

func TestFinalFrameArrivesAfterEAGAIN(t *testing.T) {
	for _, tc := range []struct {
		name  string
		drain bool
	}{{"stock", false}, {"timestamp", true}} {
		t.Run(tc.name, func(t *testing.T) {
			fds, err := unix.Socketpair(unix.AF_UNIX, unix.SOCK_SEQPACKET|unix.SOCK_NONBLOCK, 0)
			if err != nil {
				t.Fatal(err)
			}
			defer unix.Close(fds[0])
			open := true
			defer func() {
				if open {
					unix.Close(fds[1])
				}
			}()
			if err := unix.SetsockoptInt(fds[0], unix.SOL_SOCKET, unix.SO_TIMESTAMPNS, 1); err != nil {
				t.Fatal(err)
			}
			const frame = "last-ip-payload"
			var data [64]byte
			iov := unix.Iovec{Base: &data[0]}
			iov.SetLen(len(data))
			h := make([]MMsgHdr, 1)
			h[0].Msg.Iov = &iov
			h[0].Msg.SetIovlen(1)
			var observed int16
			beforePoll := func() {
				if !open {
					t.Fatal("second EAGAIN")
				}
				if err := unix.Sendmsg(fds[1], []byte(frame), nil, nil, 0); err != nil {
					t.Fatal(err)
				}
				if err := unix.Close(fds[1]); err != nil {
					t.Fatal(err)
				}
				open = false
				p := []unix.PollFd{{Fd: int32(fds[0]), Events: unix.POLLIN | unix.POLLRDHUP}}
				if _, err := unix.Poll(p, 0); err != nil {
					t.Fatal(err)
				}
				observed = p[0].Revents
				if observed&(unix.POLLIN|unix.POLLHUP) != unix.POLLIN|unix.POLLHUP {
					t.Fatalf("poll=%#x", observed)
				}
			}
			n, errno := recvMMsgUntilStopped(-1, fds[0], h, tc.drain, beforePoll)
			if open {
				t.Fatal("first receive did not report EAGAIN")
			}
			if tc.drain {
				if errno != 0 || n != 1 || h[0].Len != uint32(len(frame)) || string(data[:h[0].Len]) != frame || h[0].Msg.Flags&unix.MSG_CTRUNC == 0 {
					t.Fatalf("receive=%d len=%d errno=%v data=%q", n, h[0].Len, errno, data)
				}
			} else {
				if errno != unix.ECONNRESET || n != 0 {
					t.Fatalf("stock: %d %v", n, errno)
				}
				size, err := unix.Read(fds[0], data[:])
				if err != nil || string(data[:size]) != frame {
					t.Fatalf("queued frame=%d %v", size, err)
				}
			}
			t.Logf("DRAIN arm=%s revents=%d received=%d len=%d errno=%d", tc.name, observed, n, h[0].Len, errno)
		})
	}
}
