// Copyright 2020 The gVisor Authors.
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

package tcp_rack_test

import (
	"bytes"
	"flag"
	"fmt"
	"testing"
	"time"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/abi/linux"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/seqnum"
	"gvisor.dev/gvisor/test/packetimpact/testbench"
)

func init() {
	testbench.Initialize(flag.CommandLine)
}

const (
	// simulatedRTT is the time delay between packets sent and acked to
	// increase the RTT.
	simulatedRTT = 30 * time.Millisecond

	// numPktsForRTT is the number of packets sent and acked to establish
	// RTT.
	numPktsForRTT = 10
)

func createSACKConnection(t *testing.T) (testbench.DUT, testbench.TCPIPv4, int32, int) {
	t.Helper()
	dut := testbench.NewDUT(t)
	listenFd, remotePort := dut.CreateListener(t, unix.SOCK_STREAM, unix.IPPROTO_TCP, 1)
	t.Cleanup(func() { dut.Close(t, listenFd) })
	conn := dut.Net.NewTCPIPv4(t, testbench.TCP{DstPort: &remotePort}, testbench.TCP{SrcPort: &remotePort})
	t.Cleanup(func() { conn.Close(t) })

	// Enable SACK.
	opts := make([]byte, 40)
	optsOff := 0
	optsOff += header.EncodeNOP(opts[optsOff:])
	optsOff += header.EncodeNOP(opts[optsOff:])
	optsOff += header.EncodeSACKPermittedOption(opts[optsOff:])

	conn.ConnectWithOptions(t, opts[:optsOff])
	acceptFd, _ := dut.Accept(t, listenFd)
	t.Cleanup(func() { dut.Close(t, acceptFd) })

	// Each write must occupy one full segment: the recovery assertions count
	// packets and construct SACK ranges from their payloads. TCP options can
	// reduce the usable payload below the negotiated MSS.
	payloadSize := int(dut.GetSockOptTCPInfo(t, acceptFd).SndMss)
	if payloadSize <= 0 {
		t.Fatalf("TCP_INFO send MSS = %d, want positive", payloadSize)
	}
	return dut, conn, acceptFd, payloadSize
}

func getRTTAndRTO(t *testing.T, dut testbench.DUT, acceptFd int32) (rtt, rto time.Duration) {
	info := dut.GetSockOptTCPInfo(t, acceptFd)
	return time.Duration(info.RTT) * time.Microsecond, time.Duration(info.RTO) * time.Microsecond
}

func sendAndReceive(t *testing.T, dut testbench.DUT, conn testbench.TCPIPv4, numPkts int, acceptFd int32, payloadSize int, sendACK bool) time.Time {
	t.Helper()
	seqNum1 := *conn.RemoteSeqNum(t)
	payload := make([]byte, payloadSize)
	var lastSent time.Time
	for i, sn := 0, seqNum1; i < numPkts; i++ {
		lastSent = time.Now()
		if got, want := dut.Send(t, acceptFd, payload, 0), int32(len(payload)); got != want {
			t.Fatalf("Send #%d = %d, want %d", i+1, got, want)
		}
		if _, err := conn.ExpectData(t, &testbench.TCP{SeqNum: testbench.Uint32(uint32(sn))}, &testbench.Payload{Bytes: payload}, time.Second); err != nil {
			t.Fatalf("ExpectData #%d: %s", i+1, err)
		}
		sn.UpdateForward(seqnum.Size(payloadSize))

		if sendACK {
			time.Sleep(simulatedRTT)
			conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), AckNum: testbench.Uint32(uint32(sn))})
		}
	}
	return lastSent
}

// TestRACKTLPAllPacketsLost tests TLP when an entire flight of data is lost.
func TestRACKTLPAllPacketsLost(t *testing.T) {
	dut, conn, acceptFd, payloadSize := createSACKConnection(t)
	payload := &testbench.Payload{Bytes: make([]byte, payloadSize)}
	seqNum1 := *conn.RemoteSeqNum(t)

	// Send ACK for data packets to establish RTT.
	sendAndReceive(t, dut, conn, numPktsForRTT, acceptFd, payloadSize, true /* sendACK */)
	seqNum1.UpdateForward(seqnum.Size(numPktsForRTT * payloadSize))

	// We are not sending ACK for these packets.
	const numPkts = 5
	lastSent := sendAndReceive(t, dut, conn, numPkts, acceptFd, payloadSize, false /* sendACK */)

	// Probe Timeout (PTO) should be two times RTT. Check that the last
	// packet is retransmitted after probe timeout.
	rtt, _ := getRTTAndRTO(t, dut, acceptFd)
	pto := rtt * 2
	// We expect the 5th packet (the last unacknowledged packet) to be
	// retransmitted.
	tlpProbe := testbench.Uint32(uint32(seqNum1) + uint32((numPkts-1)*payloadSize))
	if _, err := conn.ExpectData(t, &testbench.TCP{SeqNum: tlpProbe}, payload, time.Second); err != nil {
		t.Fatalf("expected payload was not received: %s %v %v", err, rtt, pto)
	}
	diff := time.Now().Sub(lastSent)
	if diff < pto {
		t.Fatalf("expected payload was received before the probe timeout, got: %v, want: %v", diff, pto)
	}
}

// TestRACKTLPLost tests TLP when there are tail losses.
// See: https://tools.ietf.org/html/draft-ietf-tcpm-rack-08#section-7.4
func TestRACKTLPLost(t *testing.T) {
	dut, conn, acceptFd, payloadSize := createSACKConnection(t)
	payload := &testbench.Payload{Bytes: make([]byte, payloadSize)}
	seqNum1 := *conn.RemoteSeqNum(t)

	// Send ACK for data packets to establish RTT.
	sendAndReceive(t, dut, conn, numPktsForRTT, acceptFd, payloadSize, true /* sendACK */)
	seqNum1.UpdateForward(seqnum.Size(numPktsForRTT * payloadSize))

	// We are not sending ACK for these packets.
	const numPkts = 10
	lastSent := sendAndReceive(t, dut, conn, numPkts, acceptFd, payloadSize, false /* sendACK */)

	// Cumulative ACK for #[1-6] packets.
	ackNum := seqNum1.Add(seqnum.Size(6 * payloadSize))
	conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), AckNum: testbench.Uint32(uint32(ackNum))})

	// Probe Timeout (PTO) should be two times RTT. Check that the last
	// packet is retransmitted after probe timeout.
	rtt, _ := getRTTAndRTO(t, dut, acceptFd)
	pto := rtt * 2
	// We expect the 10th packet (the last unacknowledged packet) to be
	// retransmitted.
	tlpProbe := testbench.Uint32(uint32(seqNum1) + uint32((numPkts-1)*payloadSize))
	if _, err := conn.ExpectData(t, &testbench.TCP{SeqNum: tlpProbe}, payload, time.Second); err != nil {
		t.Fatalf("expected payload was not received: %s", err)
	}
	diff := time.Now().Sub(lastSent)
	if diff < pto {
		t.Fatalf("expected payload was received before the probe timeout, got: %v, want: %v", diff, pto)
	}
}

func TestRACKLostTimeoutRepair(t *testing.T) {
	dut, conn, fd, mss := createSACKConnection(t)
	sendAndReceive(t, dut, conn, numPktsForRTT, fd, mss, true /* sendACK */)
	first := *conn.RemoteSeqNum(t)
	sendAndReceive(t, dut, conn, 3, fd, mss, false /* sendACK */)
	time.Sleep(simulatedRTT / 2)
	sendAndReceive(t, dut, conn, 4, fd, mss, false /* sendACK */)

	// Withhold all ACKs until the retransmission timeout repairs the first
	// segment. A tail loss probe may have retransmitted the last one.
	head := &testbench.TCP{SeqNum: testbench.Uint32(uint32(first))}
	payload := &testbench.Payload{Bytes: make([]byte, mss)}
	before, ok := conn.ListenForFrame(t, testbench.Layers{nil, nil, head, payload}, 3*time.Second)
	if !ok {
		t.Fatal("timeout retransmission of A was absent")
	}
	headAt := time.Now()
	gotHead := before[len(before)-1]
	info := dut.GetSockOptTCPInfo(t, fd)
	if info.CaState != linux.TCP_CA_Loss || info.RTO == 0 {
		t.Fatalf("after first timeout: ca=%d rto_us=%d, want Loss with positive RTO", info.CaState, info.RTO)
	}

	peer := gotHead[2].(*testbench.TCP)
	for _, f := range before[:len(before)-1] {
		if len(f) != 4 {
			continue
		}
		tcp, ok := f[2].(*testbench.TCP)
		_, data := f[3].(*testbench.Payload)
		if ok && data && *tcp.SrcPort == *peer.SrcPort && *tcp.DstPort == *peer.DstPort {
			off := uint32(*tcp.SeqNum) - uint32(first)
			fmt.Printf("LATE_BEFORE_TIMEOUT offset=%d\n", off)
			if off < uint32(3*mss) {
				t.Fatalf("another repair of A–C preceded the first timeout: offset=%d", off)
			}
		}
	}
	// A–C originals and the timeout repair of A are treated as lost.
	// D–G have arrived; report D–F initially as in the existing native trace.
	have := [7]bool{false, false, false, true, true, true, true}
	var since time.Time
	infoAt := func(label string, i linux.TCPInfo) {
		fmt.Printf("LATE_INFO phase=%s ca=%d rto_us=%d backoff=%d retransmits=%d lost=%d sacked=%d total_retrans=%d\n",
			label, i.CaState, i.RTO, i.Backoff, i.Retransmits, i.Lost, i.Sacked, i.TotalRetrans)
	}
	fmt.Printf("LATE_DUT native=%t kernel=%s mss=%d\n", testbench.Native, dut.Uname.KernelRelease, mss)
	infoAt("first_timeout", info)
	// ListenForFrame returns preceding nonmatching frames, so waiting for A
	// cannot silently discard evidence that B or C was repaired.
	listen := func(label string, goal int, timeout time.Duration) bool {
		t.Helper()
		want := first.Add(seqnum.Size(goal * mss))
		frames, matched := conn.ListenForFrame(t, testbench.Layers{nil, nil,
			&testbench.TCP{SeqNum: testbench.Uint32(uint32(want))}, payload}, timeout)
		var seen []int
		for _, f := range frames {
			if len(f) != 4 {
				continue
			}
			tcp, ok := f[2].(*testbench.TCP)
			pl, data := f[3].(*testbench.Payload)
			if !ok || !data || *tcp.SrcPort != *peer.SrcPort || *tcp.DstPort != *peer.DstPort {
				continue
			}
			offset := uint32(*tcp.SeqNum) - uint32(first)
			if offset%uint32(mss) != 0 || !bytes.Equal(pl.Bytes, payload.Bytes) || offset/uint32(mss) >= uint32(len(have)) {
				t.Fatalf("unexpected data in %s: offset=%d length=%d MSS=%d", label, offset, len(pl.Bytes), mss)
			}
			k := int(offset) / mss
			seen = append(seen, k)
			if label == "before_sack" && k < 3 {
				t.Fatalf("another repair of A–C preceded the SACK: segment %d", k)
			}
			if label != "before_sack" {
				have[k] = true
			}
		}
		fmt.Printf("LATE_WIRE phase=%s goal=%d matched=%t elapsed_us=%d since_head_us=%d seen=%v\n",
			label, goal, matched, time.Since(since).Microseconds(), time.Since(headAt).Microseconds(), seen)
		return matched
	}
	// The impossible eighth segment lets us inspect an entire interval. Do
	// not count a packet already queued before the peer sent the SACK.
	since = time.Now()
	listen("before_sack", len(have), 2*time.Millisecond)
	sendACK := func(left, low, high int) {
		var opts [40]byte
		n := 0
		if high > low {
			n = header.EncodeSACKBlocks([]header.SACKBlock{{
				Start: first.Add(seqnum.Size(low * mss)), End: first.Add(seqnum.Size(high * mss)),
			}}, opts[:])
		}
		conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck),
			AckNum: testbench.Uint32(uint32(first.Add(seqnum.Size(left * mss)))), Options: opts[:n]})
	}
	since = time.Now()
	sendACK(0, 3, 6)
	rto := time.Duration(info.RTO) * time.Microsecond
	earlyEnd := since.Add(rto / 2)
	gotEarly := listen("sack_to_half_rto", 0, time.Until(earlyEnd))
	if gotEarly {
		infoAt("head_returned", dut.GetSockOptTCPInfo(t, fd))
		if left := time.Until(earlyEnd); left > 0 {
			listen("remainder_of_half_rto", len(have), left)
		}
	}
	infoAt("half_rto", dut.GetSockOptTCPInfo(t, fd))
	if !gotEarly {
		// No peer packet has been sent since the D–F SACK.
		if !listen("no_more_peer_input", 0, 3*time.Second) {
			t.Fatal("lost head did not return without further peer input")
		}
		infoAt("head_returned", dut.GetSockOptTCPInfo(t, fd))
	}
	if !have[0] {
		t.Fatal("no head data observed")
	}
	// Advance only over actually received contiguous data. Require any
	// other lost original to return before acknowledging beyond it.
	for {
		left := 0
		for left < len(have) && have[left] {
			left++
		}
		if left == len(have) {
			break
		}
		right := left + 1
		for right < len(have) && !have[right] {
			right++
		}
		fmt.Printf("LATE_ACK contiguous=%d sack_start=%d sack_end=%d\n", left, right, len(have))
		sendACK(left, right, len(have))
		if !listen("after_partial_ack", left, 3*time.Second) {
			t.Fatalf("segment %d never repaired after partial ACK", left)
		}
	}
	sendACK(len(have), 0, 0)
	*conn.RemoteSeqNum(t) = first.Add(seqnum.Size(len(have) * mss))
	sendAndReceive(t, dut, conn, 1, fd, mss, true /* sendACK */)
	fmt.Printf("LATE_COMPLETE segments=%d fresh=1\n", len(have))
}

// TestRACKWithSACK tests that RACK marks the packets as lost after receiving
// the ACK for retransmitted packets.
// See: https://tools.ietf.org/html/draft-ietf-tcpm-rack-08#section-8.1
func TestRACKWithSACK(t *testing.T) {
	dut, conn, acceptFd, payloadSize := createSACKConnection(t)
	payload := &testbench.Payload{Bytes: make([]byte, payloadSize)}
	seqNum1 := *conn.RemoteSeqNum(t)

	// Send ACK for data packets to establish RTT.
	sendAndReceive(t, dut, conn, numPktsForRTT, acceptFd, payloadSize, true /* sendACK */)
	seqNum1.UpdateForward(seqnum.Size(numPktsForRTT * payloadSize))

	// We are not sending ACK for these packets.
	const numPkts = 3
	sendAndReceive(t, dut, conn, numPkts, acceptFd, payloadSize, false /* sendACK */)

	time.Sleep(simulatedRTT)
	// SACK for #2 packet.
	sackBlock := make([]byte, 40)
	start := seqNum1.Add(seqnum.Size(payloadSize))
	end := start.Add(seqnum.Size(payloadSize))
	sbOff := 0
	sbOff += header.EncodeNOP(sackBlock[sbOff:])
	sbOff += header.EncodeNOP(sackBlock[sbOff:])
	sbOff += header.EncodeSACKBlocks([]header.SACKBlock{{
		start, end,
	}}, sackBlock[sbOff:])
	conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), AckNum: testbench.Uint32(uint32(seqNum1)), Options: sackBlock[:sbOff]})

	rtt, _ := getRTTAndRTO(t, dut, acceptFd)
	timeout := 2 * rtt
	// RACK marks #1 packet as lost after RTT+reorderWindow(RTT/4) and
	// retransmits it.
	if _, err := conn.ExpectData(t, &testbench.TCP{SeqNum: testbench.Uint32(uint32(seqNum1))}, payload, timeout); err != nil {
		t.Fatalf("expected payload was not received: %s", err)
	}

	// RACK ignores retransmission samples shorter than its minimum RTT
	// (RFC 8985, section 6.2). Wait longer than the warmup ACKs to leave margin
	// for scheduling jitter in that comparison.
	time.Sleep(2 * simulatedRTT)
	// ACK for #1 packet.
	conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), AckNum: testbench.Uint32(uint32(end))})

	// RACK considers transmission times of the packets to mark them lost.
	// As the 3rd packet was sent before the retransmitted 1st packet, RACK
	// marks it as lost and retransmits it..
	expectedSeqNum := testbench.Uint32(uint32(seqNum1) + uint32((numPkts-1)*payloadSize))
	if _, err := conn.ExpectData(t, &testbench.TCP{SeqNum: expectedSeqNum}, payload, timeout); err != nil {
		t.Fatalf("expected payload was not received: %s", err)
	}
}

// TestRACKWithoutReorder tests that without reordering RACK will retransmit the
// lost packets after reorder timer expires.
func TestRACKWithoutReorder(t *testing.T) {
	dut, conn, acceptFd, payloadSize := createSACKConnection(t)
	payload := &testbench.Payload{Bytes: make([]byte, payloadSize)}
	seqNum1 := *conn.RemoteSeqNum(t)

	// Send ACK for data packets to establish RTT.
	sendAndReceive(t, dut, conn, numPktsForRTT, acceptFd, payloadSize, true /* sendACK */)
	seqNum1.UpdateForward(seqnum.Size(numPktsForRTT * payloadSize))

	// We are not sending ACK for these packets.
	const numPkts = 4
	sendAndReceive(t, dut, conn, numPkts, acceptFd, payloadSize, false /* sendACK */)

	// SACK for [3,4] packets.
	sackBlock := make([]byte, 40)
	start := seqNum1.Add(seqnum.Size(2 * payloadSize))
	end := start.Add(seqnum.Size(2 * payloadSize))
	sbOff := 0
	sbOff += header.EncodeNOP(sackBlock[sbOff:])
	sbOff += header.EncodeNOP(sackBlock[sbOff:])
	sbOff += header.EncodeSACKBlocks([]header.SACKBlock{{
		start, end,
	}}, sackBlock[sbOff:])
	time.Sleep(simulatedRTT)
	conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), AckNum: testbench.Uint32(uint32(seqNum1)), Options: sackBlock[:sbOff]})

	// RACK marks #1 and #2 packets as lost and retransmits both after
	// RTT + reorderWindow. The reorderWindow initially will be a small
	// fraction of RTT.
	rtt, _ := getRTTAndRTO(t, dut, acceptFd)
	timeout := 2 * rtt
	for i, sn := 0, seqNum1; i < 2; i++ {
		if _, err := conn.ExpectData(t, &testbench.TCP{SeqNum: testbench.Uint32(uint32(sn))}, payload, timeout); err != nil {
			t.Fatalf("expected payload was not received: %s", err)
		}
		sn.UpdateForward(seqnum.Size(payloadSize))
	}
}

// TestRACKWithReorder tests that RACK will retransmit segments when there is
// reordering in the connection and reorder timer expires.
func TestRACKWithReorder(t *testing.T) {
	dut, conn, acceptFd, payloadSize := createSACKConnection(t)
	payload := &testbench.Payload{Bytes: make([]byte, payloadSize)}
	seqNum1 := *conn.RemoteSeqNum(t)

	// Send ACK for data packets to establish RTT.
	sendAndReceive(t, dut, conn, numPktsForRTT, acceptFd, payloadSize, true /* sendACK */)
	seqNum1.UpdateForward(seqnum.Size(numPktsForRTT * payloadSize))

	// Netstack's Reno halves the outstanding flight on loss. Send eight
	// segments so recovery leaves room for the second flight of four. Its three
	// SACKed segments exercise RACK's reordering window past the dup-ACK threshold.
	const firstFlightPackets = 8
	const secondFlightPackets = 4
	sendAndReceive(t, dut, conn, firstFlightPackets, acceptFd, payloadSize, false /* sendACK */)

	time.Sleep(simulatedRTT)
	// SACK in reverse order for the connection to detect reorder.
	var start seqnum.Value
	var end seqnum.Value
	for i := 0; i < firstFlightPackets-1; i++ {
		sackBlock := make([]byte, 40)
		sbOff := 0
		start = seqNum1.Add(seqnum.Size((firstFlightPackets - i - 1) * payloadSize))
		end = start.Add(seqnum.Size((i + 1) * payloadSize))
		sbOff += header.EncodeNOP(sackBlock[sbOff:])
		sbOff += header.EncodeNOP(sackBlock[sbOff:])
		sbOff += header.EncodeSACKBlocks([]header.SACKBlock{{
			start, end,
		}}, sackBlock[sbOff:])
		conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), AckNum: testbench.Uint32(uint32(seqNum1)), Options: sackBlock[:sbOff]})
	}

	// A DSACK reports duplicate bytes actually received. Wait for the first
	// segment to be retransmitted before reporting exactly that segment.
	if _, err := conn.ExpectData(t, &testbench.TCP{SeqNum: testbench.Uint32(uint32(seqNum1))}, payload, time.Second); err != nil {
		t.Fatalf("first-round retransmission was not received: %s", err)
	}
	// Require recovery before the DSACK, so the Open poll below cannot succeed
	// on stale pre-ACK state. Loss would indicate a retransmission timeout.
	beforeDSACK := dut.GetSockOptTCPInfo(t, acceptFd)
	if got, want := beforeDSACK.CaState, uint8(linux.TCP_CA_Recovery); got != want {
		t.Fatalf("congestion state after retransmission = %d, want %d (recovery)", got, want)
	}
	dsackBlock := make([]byte, 40)
	dbOff := 0
	start = seqNum1
	end = start.Add(seqnum.Size(payloadSize))
	dbOff += header.EncodeNOP(dsackBlock[dbOff:])
	dbOff += header.EncodeNOP(dsackBlock[dbOff:])
	dbOff += header.EncodeSACKBlocks([]header.SACKBlock{{
		start, end,
	}}, dsackBlock[dbOff:])
	conn.Send(t, testbench.TCP{
		Flags:   testbench.TCPFlags(header.TCPFlagAck),
		AckNum:  testbench.Uint32(uint32(seqNum1.Add(seqnum.Size(firstFlightPackets * payloadSize)))),
		Options: dsackBlock[:dbOff],
	})

	// Wait for the DSACK to finish recovery before sending the next flight.
	deadline := time.Now().Add(time.Second)
	for {
		info := dut.GetSockOptTCPInfo(t, acceptFd)
		if info.CaState == linux.TCP_CA_Open {
			if got, want := info.SndCwnd, uint32(secondFlightPackets); got < want {
				t.Fatalf("congestion window = %d, want at least %d segments", got, want)
			}
			break
		}
		if !time.Now().Before(deadline) {
			t.Fatal("DSACK did not complete recovery within one second")
		}
		time.Sleep(time.Millisecond)
	}

	seqNum1.UpdateForward(seqnum.Size(firstFlightPackets * payloadSize))
	// ExpectData tracks the end of the last matched segment, including old
	// retransmissions. Restore the cumulative receive position before expecting
	// new data, so queued first-flight retransmissions cannot match this flight.
	*conn.RemoteSeqNum(t) = seqNum1
	sendTime := time.Now()
	sendAndReceive(t, dut, conn, secondFlightPackets, acceptFd, payloadSize, false /* sendACK */)

	time.Sleep(simulatedRTT)
	// Send SACK for [2-4] packets.
	sackBlock := make([]byte, 40)
	sbOff := 0
	start = seqNum1.Add(seqnum.Size(payloadSize))
	end = start.Add(seqnum.Size((secondFlightPackets - 1) * payloadSize))
	sbOff += header.EncodeNOP(sackBlock[sbOff:])
	sbOff += header.EncodeNOP(sackBlock[sbOff:])
	sbOff += header.EncodeSACKBlocks([]header.SACKBlock{{
		start, end,
	}}, sackBlock[sbOff:])
	conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), AckNum: testbench.Uint32(uint32(seqNum1)), Options: sackBlock[:sbOff]})

	// Expect the retransmission of #1 packet after RTT+ReorderWindow.
	if _, err := conn.ExpectData(t, &testbench.TCP{SeqNum: testbench.Uint32(uint32(seqNum1))}, payload, time.Second); err != nil {
		t.Fatalf("expected payload was not received: %s", err)
	}
	rtt, _ := getRTTAndRTO(t, dut, acceptFd)
	diff := time.Now().Sub(sendTime)
	if diff < rtt {
		t.Fatalf("expected payload was received too soon, within RTT")
	}
}

// TestRACKWithLostRetransmission tests that RACK will not enter RTO when a
// retransmitted segment is lost and enters fast recovery.
func TestRACKWithLostRetransmission(t *testing.T) {
	dut, conn, acceptFd, payloadSize := createSACKConnection(t)
	payload := &testbench.Payload{Bytes: make([]byte, payloadSize)}
	seqNum1 := *conn.RemoteSeqNum(t)

	// Send ACK for data packets to establish RTT.
	sendAndReceive(t, dut, conn, numPktsForRTT, acceptFd, payloadSize, true /* sendACK */)
	seqNum1.UpdateForward(seqnum.Size(numPktsForRTT * payloadSize))

	// We are not sending ACK for these packets.
	const numPkts = 5
	sendAndReceive(t, dut, conn, numPkts, acceptFd, payloadSize, false /* sendACK */)

	// SACK for [2-5] packets.
	sackBlock := make([]byte, 40)
	start := seqNum1.Add(seqnum.Size(payloadSize))
	end := start.Add(seqnum.Size(4 * payloadSize))
	sbOff := 0
	sbOff += header.EncodeNOP(sackBlock[sbOff:])
	sbOff += header.EncodeNOP(sackBlock[sbOff:])
	sbOff += header.EncodeSACKBlocks([]header.SACKBlock{{
		start, end,
	}}, sackBlock[sbOff:])
	time.Sleep(simulatedRTT)
	conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), AckNum: testbench.Uint32(uint32(seqNum1)), Options: sackBlock[:sbOff]})

	// RACK marks #1 packet as lost and retransmits it after
	// RTT + reorderWindow. The reorderWindow is bounded between a small
	// fraction of RTT and 1 RTT.
	rtt, _ := getRTTAndRTO(t, dut, acceptFd)
	timeout := 2 * rtt
	if _, err := conn.ExpectData(t, &testbench.TCP{SeqNum: testbench.Uint32(uint32(seqNum1))}, payload, timeout); err != nil {
		t.Fatalf("expected payload was not received: %s", err)
	}

	// Send #6 packet.
	if got, want := dut.Send(t, acceptFd, payload.Bytes, 0), int32(len(payload.Bytes)); got != want {
		t.Fatalf("Send #6 = %d, want %d", got, want)
	}
	seqNum6 := seqNum1.Add(seqnum.Size(5 * payloadSize))
	if _, err := conn.ExpectData(t, &testbench.TCP{SeqNum: testbench.Uint32(uint32(seqNum6))}, payload, time.Second); err != nil {
		t.Fatalf("ExpectData #6: %s", err)
	}

	// SACK for [2-6] packets.
	sackBlock1 := make([]byte, 40)
	start = seqNum1.Add(seqnum.Size(payloadSize))
	end = start.Add(seqnum.Size(5 * payloadSize))
	sbOff1 := 0
	sbOff1 += header.EncodeNOP(sackBlock1[sbOff1:])
	sbOff1 += header.EncodeNOP(sackBlock1[sbOff1:])
	sbOff1 += header.EncodeSACKBlocks([]header.SACKBlock{{
		start, end,
	}}, sackBlock1[sbOff1:])
	time.Sleep(simulatedRTT)
	conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), AckNum: testbench.Uint32(uint32(seqNum1)), Options: sackBlock1[:sbOff1]})

	// Expect re-retransmission of #1 packet without entering an RTO.
	if _, err := conn.ExpectData(t, &testbench.TCP{SeqNum: testbench.Uint32(uint32(seqNum1))}, payload, timeout); err != nil {
		t.Fatalf("expected payload was not received: %s", err)
	}

	// Check the congestion control state.
	info := dut.GetSockOptTCPInfo(t, acceptFd)
	if info.CaState != linux.TCP_CA_Recovery {
		t.Fatalf("expected connection to be in fast recovery, want: %v got: %v", linux.TCP_CA_Recovery, info.CaState)
	}
}
