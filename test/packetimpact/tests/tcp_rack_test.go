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
	"encoding/json"
	"flag"
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

// TestApplicationLimitedLinuxWindow measures the native sender using actual
// data/ACK sequence numbers and TCP_INFO. Time is wall time on this DUT.
func TestApplicationLimitedLinuxWindow(t *testing.T) {
	for _, algorithm := range []string{"cubic", "reno"} {
		for _, schedule := range []struct {
			name string
			gap  time.Duration
		}{{"dense", 0}, {"spaced", 40 * time.Millisecond}} {
			t.Run(algorithm+"_"+schedule.name, func(t *testing.T) {
				dut, conn, fd, mss := createSACKConnection(t)
				dut.SetSockOpt(t, fd, unix.IPPROTO_TCP, unix.TCP_CONGESTION, []byte(algorithm))
				if got := string(bytes.TrimRight(dut.GetSockOpt(t, fd, unix.IPPROTO_TCP, unix.TCP_CONGESTION, 16), "\x00")); got != algorithm {
					t.Fatalf("TCP_CONGESTION = %q, want %q", got, algorithm)
				}
				dut.SetSockOptInt(t, fd, unix.IPPROTO_TCP, unix.TCP_NODELAY, 1)
				const window = 60000
				payload := make([]byte, mss)
				expected := &testbench.Payload{Bytes: payload}
				info := func() linux.TCPInfo { return dut.GetSockOptTCPInfo(t, fd) }
				ack := func(sn seqnum.Value, options []byte) {
					conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), AckNum: testbench.Uint32(uint32(sn)), WindowSize: testbench.Uint16(window), Options: options})
				}
				drain := func() linux.TCPInfo {
					t.Helper()
					start := time.Now()
					for {
						st := info()
						if st.Unacked == 0 && st.CaState == linux.TCP_CA_Open {
							return st
						}
						if time.Since(start) > time.Second {
							t.Fatalf("connection failed to drain: unacked=%d state=%d", st.Unacked, st.CaState)
						}
						time.Sleep(time.Millisecond)
					}
				}
				send := func(sn seqnum.Value) {
					t.Helper()
					if n := dut.Send(t, fd, payload, 0); int(n) != mss {
						t.Fatalf("send = %d, want %d", n, mss)
					}
					if _, err := conn.ExpectData(t, &testbench.TCP{SeqNum: testbench.Uint32(uint32(sn))}, expected, time.Second); err != nil {
						t.Fatal(err)
					}
				}
				head := *conn.RemoteSeqNum(t)
				send(head)
				head = head.Add(seqnum.Size(mss))
				time.Sleep(10 * time.Millisecond)
				ack(head, nil)
				initial := drain()
				for n := range 5 {
					send(head.Add(seqnum.Size(n * mss)))
				}
				for n := 2; n <= 4; n++ {
					var options [40]byte
					l := header.EncodeNOP(options[:])
					l += header.EncodeNOP(options[l:])
					l += header.EncodeSACKBlocks([]header.SACKBlock{{Start: head.Add(seqnum.Size(mss)), End: head.Add(seqnum.Size(n * mss))}}, options[l:])
					ack(head, options[:l])
				}
				if _, err := conn.ExpectData(t, &testbench.TCP{SeqNum: testbench.Uint32(uint32(head))}, expected, time.Second); err != nil {
					t.Fatalf("missing head repair: %v", err)
				}
				loss := info()
				if loss.CaState != linux.TCP_CA_Recovery || loss.Retransmits != 0 || loss.TotalRetrans != initial.TotalRetrans+1 {
					t.Fatalf("repair state=%d, timeouts=%d total retransmits=%d (initial %d)", loss.CaState, loss.Retransmits, loss.TotalRetrans, initial.TotalRetrans)
				}
				cursor := head.Add(seqnum.Size(5 * mss))
				ack(cursor, nil)
				base := drain()
				if base.SndCwnd < base.SndSsthresh || int(base.SndCwnd)*mss >= window {
					t.Fatalf("not unconstrained congestion avoidance: cwnd=%d threshold=%d mss=%d", base.SndCwnd, base.SndSsthresh, mss)
				}
				type sample struct {
					AtNS                             int64
					Cwnd, Unacked, NotSent, RTT, RTO uint32
				}
				samples := []sample{}
				start := time.Now()
				for range 32 {
					time.Sleep(schedule.gap)
					send(cursor)
					flight := info()
					if flight.Unacked != 1 || flight.NotSentBytes != 0 || flight.TotalRetrans != base.TotalRetrans {
						t.Fatalf("trickle unacked=%d unsent=%d total retrans=%d", flight.Unacked, flight.NotSentBytes, flight.TotalRetrans)
					}
					cursor = cursor.Add(seqnum.Size(mss))
					time.Sleep(10 * time.Millisecond)
					ack(cursor, nil)
					st := drain()
					if st.TotalRetrans != base.TotalRetrans || st.Retransmits != 0 {
						t.Fatalf("trickle retransmits=%d RTOs=%d", st.TotalRetrans, st.Retransmits)
					}
					samples = append(samples, sample{time.Since(start).Nanoseconds(), st.SndCwnd, flight.Unacked, flight.NotSentBytes, st.RTT, st.RTO})
				}
				pre := drain()
				target := int(pre.SndCwnd) * mss
				if target >= window || target <= 0 {
					t.Fatalf("receiver limits burst: %d/%d", target, window)
				}
				offer := make([]byte, target+4*mss)
				if n := dut.Send(t, fd, offer, 0); int(n) != len(offer) {
					t.Fatalf("burst Send=%d want %d", n, len(offer))
				}
				wire, frames := 0, 0
				for wire < target {
					sn := cursor.Add(seqnum.Size(wire))
					ls, err := conn.ExpectData(t, &testbench.TCP{SeqNum: testbench.Uint32(uint32(sn))}, nil, time.Second)
					if err != nil {
						t.Fatalf("burst after %d bytes: %v", wire, err)
					}
					data, ok := ls[len(ls)-1].(*testbench.Payload)
					if !ok || len(data.Bytes) == 0 || len(data.Bytes) > len(offer)-wire || !bytes.Equal(offer[wire:wire+len(data.Bytes)], data.Bytes) {
						t.Fatal("invalid burst payload")
					}
					wire += len(data.Bytes)
					frames++
				}
				full := info()
				if wire != target || full.Unacked != pre.SndCwnd || full.TotalRetrans != base.TotalRetrans || full.NotSentBytes != 4*uint32(mss) {
					t.Fatalf("burst bytes=%d expected=%d unacked=%d unsent=%d retransmits=%d", wire, target, full.Unacked, full.NotSentBytes, full.TotalRetrans)
				}
				out := struct {
					Algorithm, Profile                                                  string
					MSS, Window, BurstBytes, Frames                                     int
					InitialCwnd, PostLossCwnd, Threshold, BeforeBurstCwnd, TotalRetrans uint32
					RepairSeq, BurstSeq                                                 uint32
					GapNS                                                               int64
					Samples                                                             []sample
				}{algorithm, schedule.name, mss, window, wire, frames, initial.SndCwnd, base.SndCwnd, base.SndSsthresh, pre.SndCwnd, full.TotalRetrans, uint32(head), uint32(cursor), int64(schedule.gap), samples}
				b, err := json.Marshal(out)
				if err != nil {
					t.Fatal(err)
				}
				t.Logf("LINUX_DEMAND_RESULT %s", b)
			})
		}
	}
}
