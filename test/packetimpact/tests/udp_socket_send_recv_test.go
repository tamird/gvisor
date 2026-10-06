// Copyright 2021 The gVisor Authors.
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

package generic_dgram_socket_send_recv_test

import (
	"context"
	"net/netip"
	"testing"
	"time"

	"github.com/google/go-cmp/cmp"
	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/test/packetimpact/testbench"
)

func maxUDPPayloadSize(addr netip.Addr) int {
	if addr.Is4() {
		return maxUDPv4PayloadSize
	}
	return maxUDPv6PayloadSize
}

func TestUDP(t *testing.T) {
	runAllCombinations(t, &udpTest{})
}

type udpConn interface {
	SrcPort(*testing.T) uint16
	SendFrame(*testing.T, testbench.Layers, ...testbench.Layer)
	ListenForFrame(*testing.T, testbench.Layers, time.Duration) ([]testbench.Layers, bool)
	Close(*testing.T)
}

type udpTestEnv struct {
	socketFD int32
	conn     udpConn
	layers   testbench.Layers
}

type udpTest struct{}

func (test *udpTest) setup(t *testing.T, dut testbench.DUT, bindTo, sendTo netip.Addr, bindToDevice bool) udpTestEnv {
	t.Helper()

	var (
		socketFD                 int32
		outgoingUDP, incomingUDP testbench.UDP
	)

	// Tell the DUT to create a socket.
	if bindTo.IsValid() {
		var remotePort uint16
		socketFD, remotePort = dut.CreateBoundSocket(t, unix.SOCK_DGRAM, unix.IPPROTO_UDP, bindTo)
		outgoingUDP.DstPort = &remotePort
		incomingUDP.SrcPort = &remotePort
	} else {
		// An unbound socket will auto-bind to INADDR_ANY.
		socketFD = dut.Socket(t, unix.AF_INET6, unix.SOCK_DGRAM, unix.IPPROTO_UDP)
	}
	t.Cleanup(func() {
		dut.Close(t, socketFD)
	})

	if bindToDevice {
		dut.SetSockOpt(t, socketFD, unix.SOL_SOCKET, unix.SO_BINDTODEVICE, []byte(dut.Net.RemoteDevName))
	}

	// Create a socket on the test runner.
	var conn udpConn
	var ipLayer testbench.Layer
	if sendTo.Is4() {
		udpConn := dut.Net.NewUDPIPv4(t, outgoingUDP, incomingUDP)
		conn = &udpConn
		ipLayer = &testbench.IPv4{
			DstAddr: testbench.Address(tcpip.AddrFrom4(sendTo.As4())),
		}
	} else {
		udpConn := dut.Net.NewUDPIPv6(t, outgoingUDP, incomingUDP)
		conn = &udpConn
		ipLayer = &testbench.IPv6{
			DstAddr: testbench.Address(tcpip.AddrFrom16(sendTo.As16())),
		}
	}
	t.Cleanup(func() {
		conn.Close(t)
	})

	return udpTestEnv{
		socketFD: socketFD,
		conn:     conn,
		layers: testbench.Layers{
			expectedEthLayer(t, dut, socketFD, sendTo),
			ipLayer,
			&incomingUDP,
		},
	}
}

func (test *udpTest) Send(t *testing.T, dut testbench.DUT, bindTo, sendTo netip.Addr, bindToDevice bool) {
	wantErrno := unix.Errno(0)

	if !sendTo.Is4() {
		// If sendTo is an IPv6 address.
		if bindTo.Is4() {
			// But bindTo is an IPv4 address, we expect EAFNOSUPPORT.
			wantErrno = unix.EAFNOSUPPORT

			// TODO(gvisor.dev/issue/5967): Remove this if statement once UDPv4 sockets
			// returns EAFNOSUPPORT after calling sendto with an IPv6 address.
			if dut.Uname.IsGvisor() {
				wantErrno = unix.EINVAL
			}
		}
	} else {
		// If sendTo is an IPv4 address.
		if bindTo == dut.Net.RemoteIPv6 {
			// if bindTo is dut's IPv6 address, we expect ENETUNREACH.
			wantErrno = unix.ENETUNREACH
		}

		if !bindToDevice && bindTo != dut.Net.RemoteIPv4 && (sendTo == netip.MustParseAddr("255.255.255.255") || sendTo == netip.MustParseAddr("224.0.0.1")) {
			// if not binding to a device, bindTo is not dut's IPv4 addression and sendTo is
			// 255.255.255.255 or 224.0.0.1, we expect ENETUNERACH.
			wantErrno = unix.ENETUNREACH
		}
	}

	expectPacket := true
	// We don't expect an incoming packet if:
	// 1. sendTo is dut itself.
	if isRemoteAddr(dut, sendTo) {
		expectPacket = false
	}
	// 2. we expect an error when sending the packet.
	if wantErrno != unix.Errno(0) {
		expectPacket = false
	}

	env := test.setup(t, dut, bindTo, sendTo, bindToDevice)

	for name, payload := range map[string][]byte{
		"empty":    nil,
		"small":    []byte("hello world"),
		"random1k": testbench.GenerateRandomPayload(t, maxUDPPayloadSize(bindTo)),
	} {
		t.Run(name, func(t *testing.T) {
			var destSockaddr unix.Sockaddr
			if sendTo.Is4() {
				addr := unix.SockaddrInet4{
					Port: int(env.conn.SrcPort(t)),
					Addr: sendTo.As4(),
				}
				destSockaddr = &addr
			} else {
				addr := unix.SockaddrInet6{
					Port:   int(env.conn.SrcPort(t)),
					Addr:   sendTo.As16(),
					ZoneId: dut.Net.RemoteDevID,
				}
				destSockaddr = &addr
			}

			// Tell the DUT to send a packet out the UDP socket.
			gotRet, gotErrno := dut.SendToWithErrno(context.Background(), t, env.socketFD, payload, 0, destSockaddr)

			if gotErrno != wantErrno {
				t.Fatalf("got dut.SendToWithErrno(_, _, %d, _, _, %s) = (_, %s), want = (_, %s)", env.socketFD, sendTo, gotErrno, wantErrno)
			}
			if wantErrno != 0 {
				return
			}
			if got, want := int(gotRet), len(payload); got != want {
				t.Fatalf("got dut.SendToWithErrno(_, _, %d, _, _, %s) = (%d, _), want = (%d, _)", env.socketFD, sendTo, got, want)
			}

			// Verify the test runner received a UDP packet with the
			// correct payload.
			want := append(env.layers, &testbench.Payload{
				Bytes: payload,
			})
			if got, ok := env.conn.ListenForFrame(t, want, time.Second); !ok && expectPacket {
				t.Fatalf("did not receive expected frame matching %s\nGot frames: %s", want, got)
			} else if ok && !expectPacket {
				matchedFrame := got[len(got)-1]
				t.Fatalf("got unexpected frame matching %s\nGot frame: %s", want, matchedFrame)
			}
		})
	}
}

func (test *udpTest) Receive(t *testing.T, dut testbench.DUT, bindTo, sendTo netip.Addr, bindToDevice bool) {
	subnetBroadcast := dut.Net.SubnetBroadcast()

	expectPacket := true
	switch {
	case bindTo == sendTo:
	case bindTo == netip.IPv4Unspecified() && sendTo.Is4() && sendTo != dut.Net.LocalIPv4:
	case bindTo == netip.IPv6Unspecified() && isBroadcast(dut, sendTo):
	case bindTo == netip.IPv6Unspecified() && isRemoteAddr(dut, sendTo):
	case bindTo == subnetBroadcast && sendTo == subnetBroadcast:
	default:
		expectPacket = false
	}

	// TODO(gvisor.dev/issue/5956): Remove this if statement once gVisor
	// restricts ICMP sockets to receive only from unicast addresses.
	if (dut.Uname.IsGvisor() || dut.Uname.IsFuchsia()) && bindTo == netip.IPv6Unspecified() && sendTo == netip.MustParseAddr("224.0.0.1") {
		expectPacket = true
	}

	env := test.setup(t, dut, bindTo, sendTo, bindToDevice)
	maxPayloadSize := maxUDPPayloadSize(bindTo)

	for name, payload := range map[string][]byte{
		"empty":    nil,
		"small":    []byte("hello world"),
		"random1k": testbench.GenerateRandomPayload(t, maxPayloadSize),
	} {
		t.Run(name, func(t *testing.T) {
			// Send a UDP packet from the test runner to the DUT.
			env.conn.SendFrame(t, env.layers, &testbench.Payload{Bytes: payload})

			// Verify the behavior of the ICMP socket on the DUT.
			if expectPacket {
				// Receive one extra byte to assert the length of the
				// packet received in the case where the packet contains
				// more data than expected.
				len := int32(len(payload)) + 1
				got, want := dut.Recv(t, env.socketFD, len, 0), payload
				if diff := cmp.Diff(want, got); diff != "" {
					t.Errorf("received payload does not match sent payload, diff (-want, +got):\n%s", diff)
				}
			} else {
				// Expected receive error, set a short receive timeout.
				dut.SetSockOptTimeval(
					t,
					env.socketFD,
					unix.SOL_SOCKET,
					unix.SO_RCVTIMEO,
					&unix.Timeval{
						Sec:  1,
						Usec: 0,
					},
				)
				ret, recvPayload, errno := dut.RecvWithErrno(context.Background(), t, env.socketFD, int32(maxPayloadSize), 0)
				if errno != unix.EAGAIN || errno != unix.EWOULDBLOCK {
					t.Errorf("Recv got unexpected result, ret=%d, payload=%q, errno=%s", ret, recvPayload, errno)
				}
			}
		})
	}
}
