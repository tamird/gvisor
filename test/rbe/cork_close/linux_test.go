//go:build linux

package cork_test

import (
	"bytes"
	"io"
	"net"
	"syscall"
	"testing"
	"time"
)

// This runs on the remote worker's Linux loopback stack. Reading to EOF
// observes the data and FIN without assuming that both occupy one packet.
func TestLinuxCorkFlushBeforeFIN(t *testing.T) {
	for _, how := range []string{"close", "shutdown", "uncork_then_close"} {
		t.Run(how, func(t *testing.T) {
			l, err := net.ListenTCP("tcp4", &net.TCPAddr{IP: net.IPv4(127, 0, 0, 1)})
			if err != nil {
				t.Fatal(err)
			}
			defer l.Close()
			client, err := net.DialTCP("tcp4", nil, l.Addr().(*net.TCPAddr))
			if err != nil {
				t.Fatal(err)
			}
			defer client.Close()
			server, err := l.AcceptTCP()
			if err != nil {
				t.Fatal(err)
			}
			defer server.Close()
			rawServer, err := server.SyscallConn()
			if err != nil {
				t.Fatal(err)
			}
			rawClient, err := client.SyscallConn()
			if err != nil {
				t.Fatal(err)
			}
			cork := func(enable int) {
				t.Helper()
				var optErr error
				err := rawServer.Control(func(fd uintptr) {
					optErr = syscall.SetsockoptInt(int(fd), syscall.IPPROTO_TCP, syscall.TCP_CORK, enable)
				})
				if err != nil || optErr != nil {
					t.Fatalf("set CORK(%d): control=%v socket=%v", enable, err, optErr)
				}
			}
			if err := server.SetNoDelay(false); err != nil {
				t.Fatal(err)
			}
			cork(1)
			data := []byte("corked payload")
			start := time.Now()
			if n, err := server.Write(data); n != len(data) || err != nil {
				t.Fatalf("write=%d,%v", n, err)
			}
			var peek [1]byte
			var peekErr error
			err = rawClient.Control(func(fd uintptr) {
				_, _, peekErr = syscall.Recvfrom(int(fd), peek[:], syscall.MSG_PEEK|syscall.MSG_DONTWAIT)
			})
			if err != nil || peekErr != syscall.EAGAIN {
				t.Fatalf("before close: control=%v receive=%v, want EAGAIN", err, peekErr)
			}
			switch how {
			case "close":
				err = server.Close()
			case "shutdown":
				err = server.CloseWrite()
			case "uncork_then_close":
				cork(0)
				err = server.Close()
			}
			if err != nil {
				t.Fatal(err)
			}
			deadline := start.Add(200 * time.Millisecond)
			if err := client.SetReadDeadline(deadline); err != nil {
				t.Fatal(err)
			}
			got, err := io.ReadAll(client)
			elapsed := time.Since(start)
			t.Logf("CORK mode=%s read_to_eof_ns=%d bytes=%d", how, elapsed.Nanoseconds(), len(got))
			if err != nil || !bytes.Equal(got, data) || elapsed >= 200*time.Millisecond {
				t.Fatalf("read to EOF in %v: %q, %v; want %q before cork timer", elapsed, got, err, data)
			}
		})
	}
}
