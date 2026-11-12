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
	"flag"
	"slices"
	"sort"
	"strings"
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
	return createSACKConnectionWithMSS(t, 0)
}

func createSACKConnectionWithMSS(t *testing.T, mss uint32) (testbench.DUT, testbench.TCPIPv4, int32, int) {
	t.Helper()
	dut := testbench.NewDUT(t)
	listenFd, remotePort := dut.CreateListener(t, unix.SOCK_STREAM, unix.IPPROTO_TCP, 1)
	t.Cleanup(func() { dut.Close(t, listenFd) })
	conn := dut.Net.NewTCPIPv4(t, testbench.TCP{DstPort: &remotePort}, testbench.TCP{SrcPort: &remotePort})
	t.Cleanup(func() { conn.Close(t) })

	// Enable SACK.
	opts := make([]byte, 40)
	optsOff := 0
	if mss != 0 {
		optsOff += header.EncodeMSSOption(mss, opts)
	}
	optsOff += header.EncodeNOP(opts[optsOff:])
	optsOff += header.EncodeNOP(opts[optsOff:])
	optsOff += header.EncodeSACKPermittedOption(opts[optsOff:])

	if mss == 0 {
		conn.ConnectWithOptions(t, opts[:optsOff])
	} else {
		// Seed RACK's RTT before holding the first flight. A local handshake
		// otherwise gives a shorter probe timer than the testbench RPCs.
		conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagSyn), Options: opts[:optsOff]})
		if _, err := conn.Expect(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagSyn | header.TCPFlagAck)}, time.Second); err != nil {
			t.Fatalf("SYN-ACK: %v", err)
		}
		time.Sleep(200 * time.Millisecond)
		conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), WindowSize: testbench.Uint16(65535)})
	}
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

// TestRACKFeedbackBudget varies the second ACK during CUBIC recovery. Every
// connection starts recovery by reporting the same four received segments.
func TestRACKFeedbackBudget(t *testing.T) {
	for _, feedback := range []struct {
		name     string
		received []int
		sendACK  bool
	}{
		{name: "no_feedback"},
		{name: "repeat", sendACK: true},
		{name: "ack_one", received: []int{0}, sendACK: true},
		{name: "ack_two", received: []int{0, 1}, sendACK: true},
		{name: "sack_two", received: []int{2, 3}, sendACK: true},
		{name: "ack_four", received: []int{0, 1, 2, 3}, sendACK: true},
	} {
		t.Run(feedback.name, func(t *testing.T) {
			const terminal = 1
			const wireMSS = 536
			peerMSS := uint32(wireMSS)
			if !testbench.Native {
				// Netstack reserves 36 bytes for the optional four SACK blocks.
				peerMSS += 36
			}
			dut, conn, fd, mss := createSACKConnectionWithMSS(t, peerMSS)
			initial := dut.GetSockOptTCPInfo(t, fd)
			if mss != wireMSS || initial.RTT < 190000 || initial.SndCwnd != 10 {
				t.Fatalf("unmatched startup MSS=%d RTT=%dus cwnd=%d", mss, initial.RTT, initial.SndCwnd)
			}
			sport := uint16(dut.GetSockName(t, fd).(*unix.SockaddrInet4).Port)
			dport := uint16(conn.LocalAddr(t).Port)
			t.Logf("SETUP sport=%d dport=%d peer_mss=%d data_mss=%d cwnd=%d rtt_us=%d rto_us=%d", sport, dport, peerMSS, mss, initial.SndCwnd, initial.RTT, initial.RTO)
			dut.SetSockOpt(t, fd, unix.IPPROTO_TCP, unix.TCP_CONGESTION, []byte("cubic"))
			if cc := strings.TrimRight(string(dut.GetSockOpt(t, fd, unix.IPPROTO_TCP, unix.TCP_CONGESTION, 16)), "\x00"); cc != "cubic" {
				t.Fatalf("congestion control = %q, want cubic", cc)
			}
			dut.SetSockOptInt(t, fd, unix.IPPROTO_TCP, unix.TCP_NODELAY, 1)
			const window = 65535
			sendACK := func(ack uint32, blocks []header.SACKBlock) {
				options := make([]byte, 40)
				n := header.EncodeSACKBlocks(blocks, options)
				conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), AckNum: testbench.Uint32(ack), WindowSize: testbench.Uint16(window), Options: options[:n]})
			}
			type segment struct {
				seq    uint32
				length int
			}
			readWire := func() (data []segment) {
				frames, _ := conn.ListenForFrame(t, testbench.Layers{nil, nil, &testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagRst)}}, simulatedRTT)
				for _, frame := range frames {
					var tcp *testbench.TCP
					for _, layer := range frame {
						switch l := layer.(type) {
						case *testbench.TCP:
							if *l.SrcPort == sport && *l.DstPort == dport {
								tcp = l
								if *l.Flags&(header.TCPFlagFin|header.TCPFlagRst) != 0 {
									t.Fatal("sender closed the measured connection")
								}
							}
						case *testbench.Payload:
							if tcp != nil && len(l.Bytes) > 0 {
								data = append(data, segment{*tcp.SeqNum, len(l.Bytes)})
							}
						}
					}
				}
				return data
			}
			base := uint32(*conn.RemoteSeqNum(t))
			send := func(bytes int) {
				if n := dut.Send(t, fd, make([]byte, bytes), 0); n != int32(bytes) {
					t.Fatalf("socket write %d bytes, want %d", n, bytes)
				}
			}
			checkFlight := func(count int, measured bool) {
				next := base
				data := readWire()
				for _, seg := range data {
					if seg.seq != next || seg.length%wireMSS != 0 || measured && seg.length != wireMSS {
						t.Fatalf("unexpected wire segment seq=%d size=%d; next=%d measured=%t", seg.seq, seg.length, next, measured)
					}
					next += uint32(seg.length)
				}
				if next-base != uint32(count*wireMSS) {
					t.Fatalf("wire covered %d bytes; want %d (segments %v)", next-base, count*wireMSS, data)
				}
				t.Logf("FLIGHT base=%d mss=%d original_bytes=%d wire_frames=%d measured=%t", base, wireMSS, next-base, len(data), measured)
			}
			for _, count := range []int{10, 20} {
				send(count * wireMSS)
				checkFlight(count, false)
				base += uint32(count * wireMSS)
				sendACK(base, nil)
				until := time.Now().Add(50 * time.Millisecond)
				info := dut.GetSockOptTCPInfo(t, fd)
				for info.SndCwnd != uint32(count*2) && time.Now().Before(until) {
					time.Sleep(time.Millisecond)
					info = dut.GetSockOptTCPInfo(t, fd)
				}
				if info.SndCwnd != uint32(count*2) || info.CaState != linux.TCP_CA_Open {
					t.Fatalf("warm-up of %d MSS produced cwnd %d state %d RTT %dus", count, info.SndCwnd, info.CaState, info.RTT)
				}
			}
			dut.SetSockOptInt(t, fd, unix.SOL_SOCKET, unix.SO_SNDBUF, 256<<10)
			t.Logf("SO_SNDBUF=%d", dut.GetSockOptInt(t, fd, unix.SOL_SOCKET, unix.SO_SNDBUF))
			const flight, queued = 40, 4
			const tail, prefix = 30, 6
			for i := 0; i < flight; i++ {
				send(wireMSS)
			}
			send(queued * wireMSS)
			checkFlight(flight, true)
			// Each entry is the arrival order of the MSS. Zero means missing.
			received := make([]int, flight+queued)
			order := 0
			deliver := func(i int) {
				order++
				received[i] = order
			}
			for i := prefix; i < prefix+4-terminal; i++ {
				deliver(i)
			}
			for i := flight - terminal; i < flight; i++ {
				deliver(i)
			}
			ackReceived := func() int {
				start := 0
				for start < len(received) && received[start] != 0 {
					start++
				}
				type block struct {
					header.SACKBlock
					last int
				}
				var ranges []block
				seq := func(i int) seqnum.Value { return seqnum.Value(base + uint32(i*mss)) }
				for i := start; i < len(received); {
					if received[i] == 0 {
						i++
						continue
					}
					first, last := i, received[i]
					for i < len(received) && received[i] != 0 {
						last = max(last, received[i])
						i++
					}
					ranges = append(ranges, block{header.SACKBlock{Start: seq(first), End: seq(i)}, last})
				}
				sort.Slice(ranges, func(i, j int) bool { return ranges[i].last > ranges[j].last })
				var blocks []header.SACKBlock
				for _, r := range ranges[:min(4, len(ranges))] {
					blocks = append(blocks, r.SACKBlock)
				}
				sendACK(uint32(seq(start)), blocks)
				return start
			}
			readInterval := func() (segments []int) {
				for _, seg := range readWire() {
					offset := seg.seq - base
					end := uint64(offset) + uint64(seg.length)
					if end > uint64(len(received)*mss) || offset%uint32(mss) != 0 || seg.length%mss != 0 {
						t.Fatalf("unexpected data offset=%d length=%d", offset, seg.length)
					}
					for i := int(offset) / mss; i < int(end)/mss; i++ {
						segments = append(segments, i)
					}
				}
				return segments
			}
			t.Logf("GEOMETRY base=%d sport=%d dport=%d flight=%d mss=%d window=%d queued=%d lower=%d upper=%d middle=%d terminal=%d", base, sport, dport, flight, mss, window, queued, prefix, tail, 4-terminal, terminal)
			if cumulative := ackReceived(); cumulative != 0 {
				t.Fatalf("first cumulative ACK covered %d measured segments", cumulative)
			}
			first := readInterval()
			if len(first) < 4 || !slices.Equal(first[:4], []int{0, 1, 2, 3}) {
				t.Fatalf("peer needs the first four repairs, got %v", first)
			}
			logState := func(phase string, cumulative int, segments []int) {
				info := dut.GetSockOptTCPInfo(t, fd)
				t.Logf("BUDGET case=%s phase=%s cumulative=%d sent=%v ca=%d cwnd=%d ssthresh=%d rtos=%d backoff=%d unacked=%d sacked=%d lost=%d retrans=%d notsent=%d", feedback.name, phase, cumulative, segments, info.CaState, info.SndCwnd, info.SndSsthresh, info.Retransmits, info.Backoff, info.Unacked, info.Sacked, info.Lost, info.Retrans, info.NotSentBytes)
			}
			logState("first", 0, first)
			for _, index := range feedback.received {
				deliver(index)
			}
			cumulative := 0
			if feedback.sendACK {
				cumulative = ackReceived()
			}
			logState("second", cumulative, readInterval())
		})
	}
}
