// Copyright 2026 The gVisor Authors.
// SPDX-License-Identifier: Apache-2.0

package fdbased

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"runtime"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/adapters/gonet"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv4"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
	"gvisor.dev/gvisor/pkg/tcpip/transport/udp"
)

const port = 13719

var receiveAddr = tcpip.AddrFrom4([4]byte{192, 0, 2, 2})

func udpStack(t *testing.T) *stack.Stack {
	t.Helper()
	s := stack.New(stack.Options{
		NetworkProtocols:   []stack.NetworkProtocolFactory{ipv4.NewProtocol},
		TransportProtocols: []stack.TransportProtocolFactory{udp.NewProtocol},
	})
	t.Cleanup(s.Destroy)
	return s
}

func openReceiver(t *testing.T, s *stack.Stack, id tcpip.NICID) *gonet.UDPConn {
	t.Helper()
	addr := tcpip.ProtocolAddress{Protocol: header.IPv4ProtocolNumber, AddressWithPrefix: receiveAddr.WithPrefix()}
	if err := s.AddProtocolAddress(id, addr, stack.AddressProperties{}); err != nil {
		t.Fatal(err)
	}
	c, err := gonet.DialUDP(s, &tcpip.FullAddress{Port: port}, nil, header.IPv4ProtocolNumber)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { c.Close() })
	return c
}

func receiverFD(t *testing.T, s *stack.Stack, mode PacketDispatchMode) (int, *gonet.UDPConn, chan struct{}, stack.LinkEndpoint) {
	t.Helper()
	fds, err := unix.Socketpair(unix.AF_UNIX, unix.SOCK_DGRAM|unix.SOCK_CLOEXEC, 0)
	if err != nil {
		t.Fatal(err)
	}
	// Detach, then close the FDs separately so cleanup cannot race a blocked poll.
	t.Cleanup(func() { unix.Close(fds[0]); unix.Close(fds[1]) })
	closed := make(chan struct{}, 8)
	ep, err := New(&Options{FDs: fds[:1], MTU: mtu, ProcessorsPerChannel: 2, PacketDispatchMode: mode,
		ClosedFunc: func(tcpip.Error) { closed <- struct{}{} }})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ep.Attach(nil); ep.Close(); ep.Wait() })
	if err := s.CreateNIC(1, ep); err != nil {
		t.Fatal(err)
	}
	return fds[1], openReceiver(t, s, 1), closed, ep
}

// There is exactly one fdbased reader in this nonparallel test after SetNICStack
// has drained its predecessor. Observe it reaching the empty-data poll before
// sending: a receiver with a stale stopfd can still consume a prequeued frame.
func waitForEmptyPoll(t *testing.T, closed <-chan struct{}) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	data := make([]byte, 1<<18)
	for time.Now().Before(deadline) {
		select {
		case <-closed:
			t.Fatal("receiver exited before the next UDP packet")
		default:
		}
		n := runtime.Stack(data, true)
		for _, g := range bytes.Split(data[:n], []byte("\n\n")) {
			if bytes.Contains(g, []byte("rawfile.BlockingPollUntilStopped(")) && bytes.Contains(g, []byte("fdbased.(*endpoint).dispatchLoop(")) {
				return
			}
		}
		runtime.Gosched()
	}
	t.Fatal("receiver did not reach its idle poll")
}

func sendReceive(t *testing.T, fd int, c *gonet.UDPConn, msg string) {
	t.Helper()
	b := make([]byte, header.IPv4MinimumSize+header.UDPMinimumSize+len(msg))
	ip := header.IPv4(b)
	ip.Encode(&header.IPv4Fields{TotalLength: uint16(len(b)), TTL: 64, Protocol: uint8(header.UDPProtocolNumber),
		SrcAddr: tcpip.AddrFrom4([4]byte{192, 0, 2, 1}), DstAddr: receiveAddr})
	ip.SetChecksum(^ip.CalculateChecksum())
	u := header.UDP(b[header.IPv4MinimumSize:])
	u.Encode(&header.UDPFields{SrcPort: port + 1, DstPort: port, Length: uint16(len(u))})
	copy(u.Payload(), msg)
	if n, err := unix.Write(fd, b); err != nil || n != len(b) {
		t.Fatalf("write %d/%d: %v", n, len(b), err)
	}
	c.SetReadDeadline(time.Now().Add(3 * time.Second))
	got := make([]byte, 128)
	n, _, err := c.ReadFrom(got)
	if err != nil || string(got[:n]) != msg {
		t.Fatalf("UDP %q: read %q: %v", msg, got[:n], err)
	}
	t.Logf("received UDP %q", msg)
}

func TestLiveSaveKeepsReceivingUDP(t *testing.T) {
	for _, mode := range []PacketDispatchMode{Readv, RecvMMsg} {
		t.Run(mode.String(), func(t *testing.T) {
			s := udpStack(t)
			fd, c, ended, ep := receiverFD(t, s, mode)
			waitForEmptyPoll(t, ended)
			sendReceive(t, fd, c, "before live save")
			ep.Attach(&fakeNetworkDispatcher{})
			sendReceive(t, fd, c, "repeat attach kept the original receiver")
			waitForEmptyPoll(t, ended)
			var buf bytes.Buffer
			s.Pause()
			_, err := state.Save(context.Background(), &buf, s)
			s.Resume()
			if err != nil || buf.Len() == 0 {
				t.Fatalf("save returned %v and %d bytes", err, buf.Len())
			}
			t.Logf("checkpoint contains %d bytes", buf.Len())
			sendReceive(t, fd, c, "after live save")
		})
	}
}

func TestMoveReadv(t *testing.T)    { moveUDP(t, Readv) }
func TestMoveRecvMMsg(t *testing.T) { moveUDP(t, RecvMMsg) }

func moveUDP(t *testing.T, mode PacketDispatchMode) {
	t.Helper()
	old, peer := udpStack(t), udpStack(t)
	fd, source, ended, _ := receiverFD(t, old, mode)
	waitForEmptyPoll(t, ended)
	sendReceive(t, fd, source, "only source stack")
	id, err := old.SetNICStack(1, peer)
	if err != nil {
		t.Fatal(err)
	}
	// SetNICStack has waited for the old FD reader to finish.
	select {
	case <-ended:
	default:
		t.Fatal("move did not stop the old FD reader")
	}
	dst := openReceiver(t, peer, id)
	waitForEmptyPoll(t, ended)
	sendReceive(t, fd, dst, "first destination packet")
	waitForEmptyPoll(t, ended)
	sendReceive(t, fd, dst, "second destination packet")
	source.SetReadDeadline(time.Now())
	b := make([]byte, 128)
	n, _, e := source.ReadFrom(b)
	var ne net.Error
	if n != 0 || (!errors.Is(e, os.ErrDeadlineExceeded) && (!errors.As(e, &ne) || !ne.Timeout())) {
		t.Fatalf("source stack retained packet or returned unexpected error: %q, %v", b[:n], e)
	}
}

func TestNegativeProcessorCount(t *testing.T) {
	fds, err := unix.Socketpair(unix.AF_UNIX, unix.SOCK_DGRAM, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer unix.Close(fds[0])
	defer unix.Close(fds[1])
	ep, err := New(&Options{FDs: fds[:1], MTU: mtu, ProcessorsPerChannel: -1})
	if err == nil || ep != nil || !strings.Contains(fmt.Sprint(err), "ProcessorsPerChannel") {
		t.Fatalf("New returned %T, %v", ep, err)
	}
}
