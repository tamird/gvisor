// Copyright 2026 The gVisor Authors.
// SPDX-License-Identifier: Apache-2.0

package fdbased

import (
	"bytes"
	"context"
	"encoding/binary"
	"fmt"
	"net"
	"os"
	"runtime"
	"runtime/pprof"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
)

// packetRingFD uses only the loopback interface in the test's private netns.
func packetRingFD(t *testing.T) int {
	t.Helper()
	ifaces, err := net.Interfaces()
	if err != nil || len(ifaces) != 1 || ifaces[0].Name != "lo" {
		t.Fatalf("expected a private loopback namespace: %v, %v", ifaces, err)
	}
	ctl, err := unix.Socket(unix.AF_INET, unix.SOCK_DGRAM|unix.SOCK_CLOEXEC, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer unix.Close(ctl)
	req, err := unix.NewIfreq("lo")
	if err != nil {
		t.Fatal(err)
	}
	if err := unix.IoctlIfreq(ctl, unix.SIOCGIFFLAGS, req); err != nil {
		t.Fatalf("read loopback flags: %v", err)
	}
	req.SetUint16(req.Uint16() | unix.IFF_UP)
	if err := unix.IoctlIfreq(ctl, unix.SIOCSIFFLAGS, req); err != nil {
		t.Fatalf("bring up loopback: %v", err)
	}
	var protocol [2]byte
	binary.BigEndian.PutUint16(protocol[:], unix.ETH_P_IP)
	ipv4 := binary.NativeEndian.Uint16(protocol[:])
	fd, err := unix.Socket(unix.AF_PACKET, unix.SOCK_RAW|unix.SOCK_CLOEXEC, int(ipv4))
	if err != nil {
		t.Fatalf("create IPv4 packet socket: %v", err)
	}
	t.Cleanup(func() { unix.Close(fd) })
	if err := unix.Bind(fd, &unix.SockaddrLinklayer{Protocol: ipv4, Ifindex: ifaces[0].Index}); err != nil {
		t.Fatalf("bind packet socket to loopback: %v", err)
	}
	return fd
}

func packetRingEndpoint(t *testing.T, fd int) (*endpoint, *packetMMapDispatcher) {
	t.Helper()
	link, err := New(&Options{
		FDs:                  []int{fd},
		MTU:                  mtu,
		EthernetHeader:       true,
		Address:              tcpip.LinkAddress("\x00\x00\x00\x00\x00\x00"), // Loopback's packet-socket header.
		PacketDispatchMode:   PacketMMap,
		ProcessorsPerChannel: 2,
	})
	if err != nil {
		t.Fatalf("create mapped packet receiver: %v", err)
	}
	ep := link.(*endpoint)
	ring := ep.inboundDispatchers[0].(*packetMMapDispatcher)
	if got, want := len(ring.ringBuffer), tpBlockSize*tpBlockNR; got != want {
		t.Fatalf("mapped %d bytes, want %d", got, want)
	}
	t.Logf("PACKET_RING setup arch=%s page=%d bytes=%d", runtime.GOARCH, os.Getpagesize(), len(ring.ringBuffer))
	t.Cleanup(func() {
		if err := unix.Munmap(ring.ringBuffer); err != nil {
			t.Errorf("unmap packet ring: %v", err)
		}
	})
	return ep, ring
}

func TestPacketMMapBeforeAttach(t *testing.T) {
	fd := packetRingFD(t)
	const key = "private-packet-ring"
	pprof.Do(t.Context(), pprof.Labels(key, t.Name()), func(context.Context) {
		ep, ring := packetRingEndpoint(t, fd)
		// Release surviving stock workers after taking the profile.
		t.Cleanup(ring.mgr.close)
		ep.Close()
		ep.Wait()
	})
	var profile bytes.Buffer
	if err := pprof.Lookup("goroutine").WriteTo(&profile, 1); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(profile.String(), fmt.Sprintf("%q:%q", key, t.Name())) {
		t.Fatal("PACKET_RING worker survived Close and Wait")
	}
}

type packetRingSink struct {
	received chan struct{}
}

const ringPayload = "gvisor native packet ring on private loopback"

func (s *packetRingSink) DeliverNetworkPacket(protocol tcpip.NetworkProtocolNumber, pkt *stack.PacketBuffer) {
	if protocol == header.IPv4ProtocolNumber && bytes.HasSuffix(pkt.Data().AsRange().ToSlice(), []byte(ringPayload)) {
		select {
		case s.received <- struct{}{}:
		default:
		}
	}
}

func (*packetRingSink) DeliverLinkPacket(tcpip.NetworkProtocolNumber, *stack.PacketBuffer) {}

func TestPacketMMapDeliversUDP(t *testing.T) {
	ep, _ := packetRingEndpoint(t, packetRingFD(t))
	sink := &packetRingSink{received: make(chan struct{}, 1)}
	ep.Attach(sink)
	defer func() {
		ep.Attach(nil)
		ep.Close()
		ep.Wait()
	}()
	sock, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer sock.Close()
	if _, err := sock.WriteTo([]byte(ringPayload), sock.LocalAddr()); err != nil {
		t.Fatal(err)
	}
	select {
	case <-sink.received:
		t.Log("PACKET_RING delivered private loopback UDP")
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for the mapped receiver to deliver the UDP payload")
	}
}
