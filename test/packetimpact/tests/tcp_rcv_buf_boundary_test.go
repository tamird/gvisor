// Copyright 2026 The gVisor Authors.
// SPDX-License-Identifier: Apache-2.0

package tcp_rcv_buf_space_test

import (
	"bytes"
	"context"
	"fmt"
	"testing"
	"time"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/test/packetimpact/testbench"
)

func TestAdmissionBoundary(t *testing.T) {
	modes := []string{"original_burst", "held_prefix", "drained_prefix", "shrink_after_prefix", "large_buffer"}
	for round := 0; round < 4; round++ {
		for pos := range modes {
			mode := modes[(pos+round)%len(modes)]
			t.Run(fmt.Sprintf("%d/%s", round, mode), func(t *testing.T) {
				admissionBoundary(t, mode)
			})
		}
	}
}

func admissionBoundary(t *testing.T, mode string) {
	t.Helper()
	const packet = 1400
	dut := testbench.NewDUT(t)
	listener, port := dut.CreateListener(t, unix.SOCK_STREAM, unix.IPPROTO_TCP, 1)
	defer dut.Close(t, listener)
	conn := dut.Net.NewTCPIPv4(t, testbench.TCP{DstPort: &port}, testbench.TCP{SrcPort: &port})
	defer conn.Close(t)
	opts := []byte{header.TCPOptionNOP, header.TCPOptionNOP, header.TCPOptionSACKPermitted, 2}
	conn.ConnectWithOptions(t, opts)
	synAck := conn.SynAck(t)
	if !header.ParseSynOptions(synAck.Options, true).SACKPermitted {
		t.Fatal("peer did not negotiate SACK")
	}
	fd, _ := dut.Accept(t, listener)
	defer dut.Close(t, fd)
	base := uint32(*conn.LocalSeqNum(t))
	t.Logf("FLOW case=%s sport=%d dport=%d base=%d", t.Name(), *synAck.DstPort, *synAck.SrcPort, base)
	lateShrink := mode == "shrink_after_prefix" || mode == "large_buffer"
	if !lateShrink {
		dut.SetSockOptInt(t, fd, unix.SOL_SOCKET, unix.SO_RCVBUF, 4096)
	}
	initial := dut.GetSockOptInt(t, fd, unix.SOL_SOCKET, unix.SO_RCVBUF)
	prefix, size := 3*packet, int(initial)+1
	if lateShrink {
		prefix, size = 12*1024, 12*1024+packet
	}
	if mode == "original_burst" {
		prefix = 0
	}
	if !lateShrink && initial != 8192 {
		t.Fatalf("SO_RCVBUF = %d", initial)
	}
	payload := testbench.GenerateRandomPayload(t, size)
	send := func(data []byte) {
		for len(data) > 0 {
			n := min(packet, len(data))
			conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck)}, &testbench.Payload{Bytes: data[:n]})
			data = data[n:]
		}
	}
	window := 0
	readEarly := 0
	if prefix > 0 {
		send(payload[:prefix])
		ack, err := conn.Expect(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), AckNum: testbench.Uint32(base + uint32(prefix))}, 3*time.Second)
		if err != nil {
			t.Fatalf("ACKed prefix %d: %v", prefix, err)
		}
		window = int(*ack.WindowSize) // No scaling option was offered.
		if lateShrink && window < size-prefix {
			t.Fatalf("tail outside offered window %d", window)
		}
		if mode == "shrink_after_prefix" {
			dut.SetSockOptInt(t, fd, unix.SOL_SOCKET, unix.SO_RCVBUF, 4096)
		}
		if mode == "drained_prefix" {
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			n, data, err := dut.RecvWithErrno(ctx, t, fd, int32(prefix), unix.MSG_WAITALL)
			if int(n) != prefix || !bytes.Equal(data, payload[:prefix]) {
				t.Fatalf("prefix read got %d, err %v", n, err)
			}
			readEarly = int(n)
		}
	}
	usedLimit := dut.GetSockOptInt(t, fd, unix.SOL_SOCKET, unix.SO_RCVBUF)
	send(payload[prefix:])

	// A dropped data segment can be silent. FIN contributes one sequence
	// number but no data memory. Place it wholly below the receive frontier
	// so its unique DSACK identifies a response to this post-train packet.
	tag := base + 1
	frame := conn.CreateFrame(t, testbench.Layers{&testbench.TCP{
		Flags: testbench.TCPFlags(header.TCPFlagFin | header.TCPFlagAck), SeqNum: &tag}}, nil)
	conn.SendFrameStateless(t, frame)
	attempt := 1
	frontier := -1
	var trace []uint32
	until := time.Now().Add(time.Second)
	for frontier < 0 {
		h := conn.NextTCPObservation(t, time.Until(until))
		if h == nil {
			// Linux can rate-limit replies to out-of-window segments. A
			// retransmission still requires the same DSACK before any read.
			if attempt == 1 {
				conn.SendFrameStateless(t, frame)
				attempt++
				until = time.Now().Add(2 * time.Second)
				continue
			}
			t.Fatalf("no tagged DSACK, earlier ACK offsets %v", trace)
		}
		if *h.SrcPort != *synAck.SrcPort || *h.DstPort != *synAck.DstPort || !h.Flags.Contains(header.TCPFlagAck) {
			continue
		}
		offset := *h.AckNum - base
		if len(trace) < 32 {
			trace = append(trace, offset)
		}
		blocks := header.ParseTCPOptions(h.Options).SACKBlocks
		if len(blocks) > 0 && uint32(blocks[0].Start) == tag && uint32(blocks[0].End) == tag+1 {
			frontier = int(offset)
		}
	}
	var got []byte
	for {
		n, data, err := dut.RecvWithErrno(context.Background(), t, fd, int32(size), unix.MSG_DONTWAIT)
		if n == -1 && err == unix.EAGAIN {
			break
		}
		if n <= 0 || int(n) != len(data) || len(got)+int(n) > size-readEarly {
			t.Fatalf("drain returned %d, err %v", n, err)
		}
		got = append(got, data...)
	}
	total := readEarly + len(got)
	if total != frontier || !bytes.Equal(got, payload[readEarly:total]) {
		t.Fatalf("cumulative ACK %d, total read %d", frontier, total)
	}
	t.Logf("ADMISSION mode=%s buf=%d prefix=%d early=%d prior_window=%d sent=%d acked=%d read=%d tag=1-2 eagain=1", mode, usedLimit, prefix, readEarly, window, size, frontier, total)
}
