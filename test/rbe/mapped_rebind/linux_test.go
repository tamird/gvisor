//go:build linux

package mapped_test

import (
	"syscall"
	"testing"
)

func TestLinuxMappedConnectReleasesBothBoundFamilies(t *testing.T) {
	socket := func(family int) int {
		t.Helper()
		fd, err := syscall.Socket(family, syscall.SOCK_STREAM|syscall.SOCK_CLOEXEC, syscall.IPPROTO_TCP)
		if err != nil {
			t.Fatal(err)
		}
		return fd
	}
	must := func(err error) {
		t.Helper()
		if err != nil {
			t.Fatal(err)
		}
	}

	// Listen on a separate IPv4 loopback port throughout this experiment.
	listener := socket(syscall.AF_INET)
	defer syscall.Close(listener)
	must(syscall.Bind(listener, &syscall.SockaddrInet4{Addr: [4]byte{127, 0, 0, 1}}))
	must(syscall.Listen(listener, 1))
	serverAddr, err := syscall.Getsockname(listener)
	must(err)
	serverPort := serverAddr.(*syscall.SockaddrInet4).Port

	client := socket(syscall.AF_INET6)
	defer func() {
		if client >= 0 {
			syscall.Close(client)
		}
	}()
	must(syscall.SetsockoptInt(client, syscall.IPPROTO_IPV6, syscall.IPV6_V6ONLY, 0))
	must(syscall.Bind(client, &syscall.SockaddrInet6{}))
	bound, err := syscall.Getsockname(client)
	must(err)
	wildcard := bound.(*syscall.SockaddrInet6)
	if wildcard.Addr != ([16]byte{}) || wildcard.Port == 0 || wildcard.Port == serverPort {
		t.Fatalf("bound client: %+v, listener port %d", wildcard, serverPort)
	}
	mapped := [16]byte{10: 0xff, 11: 0xff, 12: 127, 15: 1}
	must(syscall.Connect(client, &syscall.SockaddrInet6{Addr: mapped, Port: serverPort}))
	peer, from, err := syscall.Accept(listener)
	must(err)
	defer syscall.Close(peer)
	sender := from.(*syscall.SockaddrInet4)
	if sender.Addr != ([4]byte{127, 0, 0, 1}) || sender.Port != wildcard.Port {
		t.Fatalf("IPv4 peer: %+v, want mapped client on port %d", sender, wildcard.Port)
	}
	must(syscall.SetsockoptLinger(client, syscall.SOL_SOCKET, syscall.SO_LINGER, &syscall.Linger{Onoff: 1, Linger: 0}))
	must(syscall.Close(client))
	client = -1
	must(syscall.SetsockoptTimeval(peer, syscall.SOL_SOCKET, syscall.SO_RCVTIMEO, &syscall.Timeval{Sec: 2}))
	var b [1]byte
	if n, err := syscall.Read(peer, b[:]); err != syscall.ECONNRESET {
		t.Fatalf("peer read after abortive close: n=%d, err=%v, want ECONNRESET", n, err)
	}

	for _, tc := range []struct {
		name    string
		family  int
		address syscall.Sockaddr
	}{
		{"IPv4", syscall.AF_INET, &syscall.SockaddrInet4{Port: wildcard.Port}},
		{"IPv6Only", syscall.AF_INET6, &syscall.SockaddrInet6{Port: wildcard.Port}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			fd := socket(tc.family)
			defer syscall.Close(fd)
			if tc.family == syscall.AF_INET6 {
				if err := syscall.SetsockoptInt(fd, syscall.IPPROTO_IPV6, syscall.IPV6_V6ONLY, 1); err != nil {
					t.Fatal(err)
				}
			}
			if err := syscall.Bind(fd, tc.address); err != nil {
				t.Fatalf("rebinding %s wildcard port %d: %v", tc.name, wildcard.Port, err)
			}
			t.Logf("MAPPED rebind=%s same_port=true peer_reset=true", tc.name)
		})
	}
}
