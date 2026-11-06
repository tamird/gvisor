// Copyright 2026 The gVisor Authors.
// SPDX-License-Identifier: Apache-2.0

//go:build linux && (amd64 || arm64)

package fdbased

import (
	"bytes"
	"encoding/binary"
	"errors"
	"net"
	"testing"
	"time"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/state"
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
	if err := unix.Bind(fd, &unix.SockaddrLinklayer{Protocol: ipv4, Ifindex: ifaces[0].Index}); err != nil {
		t.Fatalf("bind packet socket to loopback: %v", err)
	}
	return fd
}

func nativeRing(t *testing.T) (*endpoint, *packetMMapDispatcher, func()) {
	t.Helper()
	fd := packetRingFD(t)
	closeFD := func() {
		if fd >= 0 {
			if err := unix.Close(fd); err != nil {
				t.Fatal(err)
			}
			fd = -1
		}
	}
	t.Cleanup(closeFD)
	link, err := New(&Options{
		FDs: []int{fd}, MTU: mtu, EthernetHeader: true,
		PacketDispatchMode: PacketMMap, ProcessorsPerChannel: 1,
		Address: tcpip.LinkAddress("\x00\x00\x00\x00\x00\x00"),
	})
	if err != nil {
		t.Fatal(err)
	}
	ep := link.(*endpoint)
	ring := ep.inboundDispatchers[0].(*packetMMapDispatcher)
	original := ring.ringBuffer
	t.Logf("ring-bytes=%d", len(original))
	t.Cleanup(func() {
		ep.Attach(nil)
		ep.Close()
		// The unmodified implementation leaves this VMA live.
		if unix.Msync(original, unix.MS_ASYNC) == nil {
			unix.Munmap(original)
		}
	})
	return ep, ring, closeFD
}

func probeMapping(t *testing.T, when string, b []byte, want error) {
	t.Helper()
	// MS_ASYNC tests the original address range without reading its bytes.
	err := unix.Msync(b, unix.MS_ASYNC)
	t.Logf("mapping %s: %v", when, err)
	if !errors.Is(err, want) {
		t.Errorf("msync %s: got %v, want %v", when, err, want)
	}
}

const marker = "gvisor AF_PACKET retained UDP packet after receiver teardown"

type retainingPacketSink struct{ received chan *stack.PacketBuffer }

func (s *retainingPacketSink) DeliverNetworkPacket(proto tcpip.NetworkProtocolNumber, pkt *stack.PacketBuffer) {
	if proto != header.IPv4ProtocolNumber || !bytes.HasSuffix(pkt.Data().AsRange().ToSlice(), []byte(marker)) {
		return
	}
	held := pkt.IncRef()
	select {
	case s.received <- held:
	default:
		held.DecRef()
	}
}

func (*retainingPacketSink) DeliverLinkPacket(tcpip.NetworkProtocolNumber, *stack.PacketBuffer) {}

func TestPacketRingMapping(t *testing.T) {
	t.Run("never-attached", func(t *testing.T) {
		ep, ring, closeFD := nativeRing(t)
		original := ring.ringBuffer
		probeMapping(t, "before-close", original, nil)
		ep.Close()
		probeMapping(t, "after-close", original, unix.ENOMEM)
		ep.Close()
		closeFD()
		probeMapping(t, "after-double-close-and-fd-close", original, unix.ENOMEM)
	})
	t.Run("after-delivery", func(t *testing.T) {
		ep, ring, _ := nativeRing(t)
		original := ring.ringBuffer
		probeMapping(t, "before-attach", original, nil)
		sink := &retainingPacketSink{received: make(chan *stack.PacketBuffer, 1)}
		ep.Attach(sink)
		sock, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
		if err != nil {
			t.Fatal(err)
		}
		defer sock.Close()
		if _, err := sock.WriteTo([]byte(marker), sock.LocalAddr()); err != nil {
			t.Fatal(err)
		}
		var held *stack.PacketBuffer
		select {
		case held = <-sink.received:
		case <-time.After(5 * time.Second):
			t.Fatal("no private UDP delivery")
		}
		defer held.DecRef()
		ep.Attach(nil)
		ep.Wait()
		select {
		case extra := <-sink.received:
			extra.DecRef()
		default:
		}
		probeMapping(t, "after-detach", original, nil)
		ep.Close()
		probeMapping(t, "after-close", original, unix.ENOMEM)
		got := held.Data().AsRange().ToSlice()
		if !bytes.HasSuffix(got, []byte(marker)) {
			t.Fatalf("held datagram no longer ends in %q", marker)
		}
		t.Logf("retained-packet=%d bytes with expected UDP marker", len(got))
		ep.Close()
		probeMapping(t, "after-double-close", original, unix.ENOMEM)
	})
	t.Run("restored", func(t *testing.T) {
		ep, ring, _ := nativeRing(t)
		original := ring.ringBuffer
		// Isolate the two host resources from unrelated processor goroutines.
		saved := packetMMapDispatcher{fd: ring.fd, StopFD: ring.StopFD, ringBuffer: original}
		var image bytes.Buffer
		if _, err := state.Save(t.Context(), &image, &saved); err != nil {
			t.Fatal(err)
		}
		var restored packetMMapDispatcher
		if _, err := state.Load(t.Context(), &image, &restored); err != nil {
			t.Fatal(err)
		}
		t.Logf("restored-ring-bytes=%d stop-fd=%d", len(restored.ringBuffer), restored.EFD)
		if len(restored.ringBuffer) != 0 || restored.EFD != -1 {
			t.Errorf("restored dispatcher held live-resource state")
		}
		restored.close()
		restored.close()
		probeMapping(t, "after-restored-close", original, nil)
		ep.Close()
		probeMapping(t, "after-owner-close", original, unix.ENOMEM)
	})
}
