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
	"fmt"
	"os"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/buffer"
	"gvisor.dev/gvisor/pkg/refs"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/faketime"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/seqnum"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp/test/e2e"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp/testing/context"
	"gvisor.dev/gvisor/pkg/test/testutil"
)

const (
	maxPayload       = 10
	maxTCPOptionSize = 40
	mtu              = header.TCPMinimumSize + header.IPv4MinimumSize + maxTCPOptionSize + maxPayload
)

// TestRACKUpdate tests the RACK related fields are updated when an ACK is
// received on a SACK enabled connection.
func TestRACKUpdate(t *testing.T) {
	var xmitTime tcpip.MonotonicTime
	probeDone := make(chan struct{})
	probe := func(state *tcp.TCPEndpointState) {
		// Validate that the endpoint Sender.RACKState is what we expect.
		if state.Sender.RACKState.XmitTime.Before(xmitTime) {
			t.Fatalf("RACK transmit time failed to update when an ACK is received")
		}

		gotSeq := state.Sender.RACKState.EndSequence
		wantSeq := state.Sender.SndNxt
		if !gotSeq.LessThanEq(wantSeq) || gotSeq.LessThan(wantSeq) {
			t.Fatalf("RACK sequence number failed to update, got: %v, but want: %v", gotSeq, wantSeq)
		}

		if state.Sender.RACKState.RTT == 0 {
			t.Fatalf("RACK RTT failed to update when an ACK is received, got RACKState.RTT == 0 want != 0")
		}
		close(probeDone)
	}

	c := context.NewWithProbe(t, uint32(mtu), probe)
	defer c.Cleanup()

	e2e.SetStackSACKPermitted(t, c, true)
	e2e.CreateConnectedWithSACKAndTS(c)

	data := make([]byte, maxPayload)
	for i := range data {
		data[i] = byte(i)
	}

	// Write the data.
	xmitTime = c.Stack().Clock().NowMonotonic()
	var r bytes.Reader
	r.Reset(data)
	if _, err := c.EP.Write(&r, tcpip.WriteOptions{}); err != nil {
		t.Fatalf("Write failed: %s", err)
	}

	bytesRead := 0
	c.ReceiveAndCheckPacketWithOptions(data, bytesRead, maxPayload, e2e.TSOptionSize)
	bytesRead += maxPayload
	c.SendAck(seqnum.Value(context.TestInitialSequenceNumber).Add(1), bytesRead)

	// Wait for the probe function to finish processing the ACK before the
	// test completes.
	<-probeDone
}

// TestRACKDetectReorder tests that RACK detects packet reordering.
func TestRACKDetectReorder(t *testing.T) {
	clock := faketime.NewManualClock()
	states := make(chan tcp.TCPSenderState, 3)
	probe := func(state *tcp.TCPEndpointState) {
		select {
		case states <- state.Sender:
		default:
		}
	}
	nextState := func() tcp.TCPSenderState {
		t.Helper()
		select {
		case state := <-states:
			return state
		case <-time.After(5 * time.Second):
			t.Fatal("timed out waiting for ACK processing")
			return tcp.TCPSenderState{}
		}
	}

	c := context.NewWithOpts(t, context.Options{
		EnableV4: true,
		MTU:      mtu,
		Clock:    clock,
		Probe:    probe,
	})
	defer c.Cleanup()

	e2e.SetStackSACKPermitted(t, c, true)
	e2e.CreateConnectedWithSACKAndTS(c)
	data := make([]byte, 3*maxPayload)
	for i := range data {
		data[i] = byte(i)
	}

	// Establish a positive RTT before testing reordering: RACK caps its
	// reordering window by SRTT. The manual-clock handshake takes no time.
	var r bytes.Reader
	r.Reset(data[:maxPayload])
	if _, err := c.EP.Write(&r, tcpip.WriteOptions{}); err != nil {
		t.Fatalf("Write failed: %s", err)
	}
	c.ReceiveAndCheckPacketWithOptions(data, 0, maxPayload, e2e.TSOptionSize)
	const rtt = 100 * time.Millisecond
	clock.Advance(rtt)
	seq := seqnum.Value(context.TestInitialSequenceNumber).Add(1)
	c.SendAck(seq, maxPayload)
	state := nextState()
	if got := state.RTTState.SRTT; got <= 0 {
		t.Fatalf("SRTT = %s, want a positive RTT estimate", got)
	}
	if got, want := state.RACKState.RTT, rtt; got != want {
		t.Fatalf("RACK RTT = %s, want %s", got, want)
	}

	// Send two packets, then selectively acknowledge only the second.
	r.Reset(data[maxPayload:])
	if _, err := c.EP.Write(&r, tcpip.WriteOptions{}); err != nil {
		t.Fatalf("Write failed: %s", err)
	}
	for offset := maxPayload; offset < len(data); offset += maxPayload {
		c.ReceiveAndCheckPacketWithOptions(data, offset, maxPayload, e2e.TSOptionSize)
	}
	clock.Advance(rtt)
	start := c.IRS.Add(2*maxPayload + 1)
	end := start.Add(maxPayload)
	c.SendAckWithSACK(seq, maxPayload, []header.SACKBlock{{start, end}})
	state = nextState()
	if got, want := state.RACKState.FACK, end; got != want {
		t.Fatalf("FACK after SACK = %v, want %v", got, want)
	}
	if state.RACKState.Reord {
		t.Fatal("RACK detected reordering before the missing packet was acknowledged")
	}
	if got := state.RACKState.ReoWnd; got <= 0 {
		t.Fatalf("reordering window = %s, want a positive window", got)
	}

	// ACK the missing packet within the reordering window, before it is
	// retransmitted. Step 3 detects this as reordering without a DSACK:
	// https://www.rfc-editor.org/rfc/rfc8985.html#section-6.2
	c.SendAck(seq, len(data))
	state = nextState()
	if got, want := state.RACKState.FACK, end; got != want {
		t.Fatalf("FACK after cumulative ACK = %v, want %v", got, want)
	}
	if !state.RACKState.Reord {
		t.Fatal("RACK did not detect reordering of the never-retransmitted packet")
	}
	if state.RACKState.DSACKSeen {
		t.Fatal("RACK reported a DSACK when none was sent")
	}
	if got, want := c.Stack().Stats().TCP.Retransmits.Value(), uint64(0); got != want {
		t.Fatalf("retransmitted packets = %d, want %d", got, want)
	}
}

const (
	validDSACKDetected   = 1
	failedToDetectDSACK  = 2
	invalidDSACKDetected = 3
)

func dsackSeenCheckerProbe(t *testing.T, numACK int, probeDone chan int) tcp.TCPProbeFunc {
	var n int
	return func(state *tcp.TCPEndpointState) {
		// Validate that RACK detects DSACK.
		n++
		if n < numACK {
			if state.Sender.RACKState.DSACKSeen {
				probeDone <- invalidDSACKDetected
			}
			return
		}

		if !state.Sender.RACKState.DSACKSeen {
			probeDone <- failedToDetectDSACK
			return
		}
		probeDone <- validDSACKDetected
	}
}

// TestRACKTLPRecovery tests that RACK sends a tail loss probe (TLP) in the
// case of a tail loss. This simulates a situation where the TLP is able to
// insinuate the SACK holes and sender is able to retransmit the rest.
func TestRACKTLPRecovery(t *testing.T) {
	c := context.New(t, uint32(mtu))
	defer c.Cleanup()

	// Send 8 packets.
	numPackets := 8
	data := e2e.SendAndReceiveWithSACK(t, c, maxPayload, numPackets, true /* enableRACK */)

	// Packets [6-8] are lost. Send cumulative ACK for [1-5].
	seq := seqnum.Value(context.TestInitialSequenceNumber).Add(1)
	bytesRead := 5 * maxPayload
	c.SendAck(seq, bytesRead)

	// PTO should fire and send #8 packet as a TLP.
	c.ReceiveAndCheckPacketWithOptions(data, 7*maxPayload, maxPayload, e2e.TSOptionSize)
	var info tcpip.TCPInfoOption
	if err := c.EP.GetSockOpt(&info); err != nil {
		t.Fatalf("GetSockOpt failed: %v", err)
	}

	// Send the SACK after RTT because RACK RFC states that if the ACK for a
	// retransmission arrives before the smoothed RTT then the sender should not
	// update RACK state as it could be a spurious inference.
	time.Sleep(info.RTT)

	// Okay, let the sender know we got #8 using a SACK block.
	eighthPStart := c.IRS.Add(1 + seqnum.Size(7*maxPayload))
	eighthPEnd := eighthPStart.Add(maxPayload)
	c.SendAckWithSACK(seq, bytesRead, []header.SACKBlock{{eighthPStart, eighthPEnd}})

	// The sender should be entering RACK based loss-recovery and sending #6 and
	// #7 one after another.
	c.ReceiveAndCheckPacketWithOptions(data, bytesRead, maxPayload, e2e.TSOptionSize)
	bytesRead += maxPayload
	c.ReceiveAndCheckPacketWithOptions(data, bytesRead, maxPayload, e2e.TSOptionSize)
	bytesRead += 2 * maxPayload
	c.SendAck(seq, bytesRead)

	metricPollFn := func() error {
		tcpStats := c.Stack().Stats().TCP
		stats := []struct {
			stat *tcpip.StatCounter
			name string
			want uint64
		}{
			// One fast retransmit after the SACK.
			{tcpStats.FastRetransmit, "stats.TCP.FastRetransmit", 1},
			// Recovery should be SACK recovery.
			{tcpStats.SACKRecovery, "stats.TCP.SACKRecovery", 1},
			// Packets 6, 7 and 8 were retransmitted.
			{tcpStats.Retransmits, "stats.TCP.Retransmits", 3},
			// TLP recovery should have been detected.
			{tcpStats.TLPRecovery, "stats.TCP.TLPRecovery", 1},
			// No RTOs should have occurred.
			{tcpStats.Timeouts, "stats.TCP.Timeouts", 0},
		}
		for _, s := range stats {
			if got, want := s.stat.Value(), s.want; got != want {
				return fmt.Errorf("got %s.Value() = %d, want = %d", s.name, got, want)
			}
		}
		return nil
	}
	if err := testutil.Poll(metricPollFn, 1*time.Second); err != nil {
		t.Error(err)
	}
}

// TestRACKTLPFallbackRTO tests that RACK sends a tail loss probe (TLP) in the
// case of a tail loss. This simulates a situation where either the TLP or its
// ACK is lost. The sender should retransmit when RTO fires.
func TestRACKTLPFallbackRTO(t *testing.T) {
	for _, algorithm := range []string{"reno", "cubic"} {
		t.Run(algorithm, func(t *testing.T) {
			c := context.New(t, uint32(mtu))
			defer c.Cleanup()

			cc := tcpip.CongestionControlOption(algorithm)
			if err := c.Stack().SetTransportProtocolOption(tcp.ProtocolNumber, &cc); err != nil {
				t.Fatalf("SetTransportProtocolOption(%s): %s", algorithm, err)
			}

			// Send 8 packets.
			numPackets := 8
			data := e2e.SendAndReceiveWithSACK(t, c, maxPayload, numPackets, true /* enableRACK */)

			// Packets [6-8] are lost. Send cumulative ACK for [1-5].
			seq := seqnum.Value(context.TestInitialSequenceNumber).Add(1)
			bytesRead := 5 * maxPayload
			c.SendAck(seq, bytesRead)

			// PTO should fire and send #8 packet as a TLP.
			c.ReceiveAndCheckPacketWithOptions(data, 7*maxPayload, maxPayload, e2e.TSOptionSize)

			// Either the TLP or the ACK the receiver sent with SACK blocks was lost.

			// Confirm that RTO fires and retransmits packet #6.
			c.ReceiveAndCheckPacketWithOptions(data, bytesRead, maxPayload, e2e.TSOptionSize)

			metricPollFn := func() error {
				tcpStats := c.Stack().Stats().TCP
				stats := []struct {
					stat *tcpip.StatCounter
					name string
					want uint64
				}{
					// No fast retransmits happened.
					{tcpStats.FastRetransmit, "stats.TCP.FastRetransmit", 0},
					// No SACK recovery happened.
					{tcpStats.SACKRecovery, "stats.TCP.SACKRecovery", 0},
					// TLP was unsuccessful.
					{tcpStats.TLPRecovery, "stats.TCP.TLPRecovery", 0},
					// RTO should have fired.
					{tcpStats.Timeouts, "stats.TCP.Timeouts", 1},
				}
				for _, s := range stats {
					if got, want := s.stat.Value(), s.want; got != want {
						return fmt.Errorf("got %s.Value() = %d, want = %d", s.name, got, want)
					}
				}
				return nil
			}
			if err := testutil.Poll(metricPollFn, 1*time.Second); err != nil {
				t.Error(err)
			}

			// ACK the retransmission and drain the rest of the original flight.
			// Reaching the RTO alone would not detect a sender stuck in recovery.
			bytesRead += maxPayload
			c.SendAck(seq, bytesRead)
			for bytesRead < len(data) {
				c.ReceiveAndCheckPacketWithOptions(data, bytesRead, maxPayload, e2e.TSOptionSize)
				bytesRead += maxPayload
			}
			c.SendAck(seq, bytesRead)
		})
	}
}

// TestNoTLPRecoveryOnDSACK tests the scenario where the sender speculates a
// tail loss and sends a TLP. Everything is received and acked. The probe
// segment is DSACKed. No fast recovery should be triggered in this case.
func TestNoTLPRecoveryOnDSACK(t *testing.T) {
	c := context.New(t, uint32(mtu))
	defer c.Cleanup()

	// Send 8 packets.
	numPackets := 8
	data := e2e.SendAndReceiveWithSACK(t, c, maxPayload, numPackets, true /* enableRACK */)

	// Packets [1-5] are received first. [6-8] took a detour and will take a
	// while to arrive. Ack [1-5].
	seq := seqnum.Value(context.TestInitialSequenceNumber).Add(1)
	bytesRead := 5 * maxPayload
	c.SendAck(seq, bytesRead)

	// The tail loss probe (#8 packet) is received.
	c.ReceiveAndCheckPacketWithOptions(data, 7*maxPayload, maxPayload, e2e.TSOptionSize)

	// Now that all 8 packets are received + duplicate 8th packet, send ack.
	bytesRead += 3 * maxPayload
	eighthPStart := c.IRS.Add(1 + seqnum.Size(7*maxPayload))
	eighthPEnd := eighthPStart.Add(maxPayload)
	c.SendAckWithSACK(seq, bytesRead, []header.SACKBlock{{eighthPStart, eighthPEnd}})

	// Wait for RTO and make sure that nothing else is received.
	var info tcpip.TCPInfoOption
	if err := c.EP.GetSockOpt(&info); err != nil {
		t.Fatalf("GetSockOpt failed: %v", err)
	}
	var p *buffer.View
	if p = c.GetPacketWithTimeout(info.RTO); p != nil {
		t.Errorf("received an unexpected packet: %v", p)
		p.Release()
	}

	metricPollFn := func() error {
		tcpStats := c.Stack().Stats().TCP
		stats := []struct {
			stat *tcpip.StatCounter
			name string
			want uint64
		}{
			// Make sure no recovery was entered.
			{tcpStats.FastRetransmit, "stats.TCP.FastRetransmit", 0},
			{tcpStats.SACKRecovery, "stats.TCP.SACKRecovery", 0},
			{tcpStats.TLPRecovery, "stats.TCP.TLPRecovery", 0},
			// RTO should not have fired.
			{tcpStats.Timeouts, "stats.TCP.Timeouts", 0},
			// Only #8 was retransmitted.
			{tcpStats.Retransmits, "stats.TCP.Retransmits", 1},
		}
		for _, s := range stats {
			if got, want := s.stat.Value(), s.want; got != want {
				return fmt.Errorf("got %s.Value() = %d, want = %d", s.name, got, want)
			}
		}
		return nil
	}
	if err := testutil.Poll(metricPollFn, 1*time.Second); err != nil {
		t.Error(err)
	}
}

// TestNoTLPOnSACK tests the scenario where there is not exactly a tail loss
// due to the presence of multiple SACK holes. In such a scenario, TLP should
// not be sent.
func TestNoTLPOnSACK(t *testing.T) {
	c := context.New(t, uint32(mtu))
	defer c.Cleanup()

	// Send 8 packets.
	numPackets := 8
	data := e2e.SendAndReceiveWithSACK(t, c, maxPayload, numPackets, true /* enableRACK */)

	// Packets [1-5] and #7 were received. #6 and #8 were dropped.
	seq := seqnum.Value(context.TestInitialSequenceNumber).Add(1)
	bytesRead := 5 * maxPayload
	seventhStart := c.IRS.Add(1 + seqnum.Size(6*maxPayload))
	seventhEnd := seventhStart.Add(maxPayload)
	c.SendAckWithSACK(seq, bytesRead, []header.SACKBlock{{seventhStart, seventhEnd}})

	// The sender should retransmit #6. If the sender sends a TLP, then #8 will
	// received and fail this test.
	c.ReceiveAndCheckPacketWithOptions(data, 5*maxPayload, maxPayload, e2e.TSOptionSize)

	metricPollFn := func() error {
		tcpStats := c.Stack().Stats().TCP
		stats := []struct {
			stat *tcpip.StatCounter
			name string
			want uint64
		}{
			// #6 was retransmitted due to SACK recovery.
			{tcpStats.FastRetransmit, "stats.TCP.FastRetransmit", 1},
			{tcpStats.SACKRecovery, "stats.TCP.SACKRecovery", 1},
			{tcpStats.TLPRecovery, "stats.TCP.TLPRecovery", 0},
			// RTO should not have fired.
			{tcpStats.Timeouts, "stats.TCP.Timeouts", 0},
			// Only #6 was retransmitted.
			{tcpStats.Retransmits, "stats.TCP.Retransmits", 1},
		}
		for _, s := range stats {
			if got, want := s.stat.Value(), s.want; got != want {
				return fmt.Errorf("got %s.Value() = %d, want = %d", s.name, got, want)
			}
		}
		return nil
	}
	if err := testutil.Poll(metricPollFn, 1*time.Second); err != nil {
		t.Error(err)
	}
}

// TestRACKOnePacketTailLoss tests the trivial case of a tail loss of only one
// packet. The probe should itself repairs the loss instead of having to go
// into any recovery.
func TestRACKOnePacketTailLoss(t *testing.T) {
	c := context.New(t, uint32(mtu))
	defer c.Cleanup()

	// Send 3 packets.
	numPackets := 3
	data := e2e.SendAndReceiveWithSACK(t, c, maxPayload, numPackets, true /* enableRACK */)

	// Packets [1-2] are received. #3 is lost.
	seq := seqnum.Value(context.TestInitialSequenceNumber).Add(1)
	bytesRead := 2 * maxPayload
	c.SendAck(seq, bytesRead)

	// PTO should fire and send #3 packet as a TLP.
	c.ReceiveAndCheckPacketWithOptions(data, 2*maxPayload, maxPayload, e2e.TSOptionSize)
	bytesRead += maxPayload
	c.SendAck(seq, bytesRead)

	metricPollFn := func() error {
		tcpStats := c.Stack().Stats().TCP
		stats := []struct {
			stat *tcpip.StatCounter
			name string
			want uint64
		}{
			// #3 was retransmitted as TLP.
			{tcpStats.FastRetransmit, "stats.TCP.FastRetransmit", 0},
			{tcpStats.SACKRecovery, "stats.TCP.SACKRecovery", 1},
			{tcpStats.TLPRecovery, "stats.TCP.TLPRecovery", 0},
			// RTO should not have fired.
			{tcpStats.Timeouts, "stats.TCP.Timeouts", 0},
			// Only #3 was retransmitted.
			{tcpStats.Retransmits, "stats.TCP.Retransmits", 1},
		}
		for _, s := range stats {
			if got, want := s.stat.Value(), s.want; got != want {
				return fmt.Errorf("got %s.Value() = %d, want = %d", s.name, got, want)
			}
		}
		return nil
	}
	if err := testutil.Poll(metricPollFn, 1*time.Second); err != nil {
		t.Error(err)
	}
}

// TestRACKDetectDSACK tests that RACK detects DSACK with duplicate segments.
// See: https://tools.ietf.org/html/rfc2883#section-4.1.1.
func TestRACKDetectDSACK(t *testing.T) {
	probeDone := make(chan int)
	const ackNumToVerify = 2
	probe := dsackSeenCheckerProbe(t, ackNumToVerify, probeDone)

	c := context.NewWithProbe(t, uint32(mtu), probe)
	defer c.Cleanup()

	numPackets := 8
	data := e2e.SendAndReceiveWithSACK(t, c, maxPayload, numPackets, true /* enableRACK */)

	// Cumulative ACK for [1-5] packets and SACK #8 packet (to prevent TLP).
	seq := seqnum.Value(context.TestInitialSequenceNumber).Add(1)
	bytesRead := 5 * maxPayload
	eighthPStart := c.IRS.Add(1 + seqnum.Size(7*maxPayload))
	eighthPEnd := eighthPStart.Add(maxPayload)
	c.SendAckWithSACK(seq, bytesRead, []header.SACKBlock{{eighthPStart, eighthPEnd}})

	// Expect retransmission of #6 packet after RTO expires.
	c.ReceiveAndCheckPacketWithOptions(data, bytesRead, maxPayload, e2e.TSOptionSize)

	// Send DSACK block for #6 packet indicating both
	// initial and retransmitted packet are received and
	// packets [1-8] are received.
	start := c.IRS.Add(1 + seqnum.Size(bytesRead))
	end := start.Add(maxPayload)
	bytesRead += 3 * maxPayload
	c.SendAckWithSACK(seq, bytesRead, []header.SACKBlock{{start, end}})

	// Wait for the probe function to finish processing the
	// ACK before the test completes.
	err := <-probeDone
	switch err {
	case failedToDetectDSACK:
		t.Fatalf("RACK DSACK detection failed")
	case invalidDSACKDetected:
		t.Fatalf("RACK DSACK detected when there is no duplicate SACK")
	}

	metricPollFn := func() error {
		tcpStats := c.Stack().Stats().TCP
		stats := []struct {
			stat *tcpip.StatCounter
			name string
			want uint64
		}{
			// Check DSACK was received for one segment.
			{tcpStats.SegmentsAckedWithDSACK, "stats.TCP.SegmentsAckedWithDSACK", 1},
		}
		for _, s := range stats {
			if got, want := s.stat.Value(), s.want; got != want {
				return fmt.Errorf("got %s.Value() = %d, want = %d", s.name, got, want)
			}
		}
		return nil
	}

	if err := testutil.Poll(metricPollFn, 1*time.Second); err != nil {
		t.Error(err)
	}
}

// TestRACKDetectDSACKWithOutOfOrder tests that RACK detects DSACK with out of
// order segments.
// See: https://tools.ietf.org/html/rfc2883#section-4.1.2.
func TestRACKDetectDSACKWithOutOfOrder(t *testing.T) {
	probeDone := make(chan int)
	const ackNumToVerify = 2
	probe := dsackSeenCheckerProbe(t, ackNumToVerify, probeDone)

	c := context.NewWithProbe(t, uint32(mtu), probe)
	defer c.Cleanup()

	numPackets := 10
	data := e2e.SendAndReceiveWithSACK(t, c, maxPayload, numPackets, true /* enableRACK */)

	// Cumulative ACK for [1-5] packets and SACK for #7 packet (to prevent TLP).
	seq := seqnum.Value(context.TestInitialSequenceNumber).Add(1)
	bytesRead := 5 * maxPayload
	seventhPStart := c.IRS.Add(1 + seqnum.Size(6*maxPayload))
	seventhPEnd := seventhPStart.Add(maxPayload)
	c.SendAckWithSACK(seq, bytesRead, []header.SACKBlock{{seventhPStart, seventhPEnd}})

	// Expect retransmission of #6 packet.
	c.ReceiveAndCheckPacketWithOptions(data, bytesRead, maxPayload, e2e.TSOptionSize)

	// Send DSACK block for #6 packet indicating both
	// initial and retransmitted packet are received and
	// packets [1-7] are received.
	start := c.IRS.Add(1 + seqnum.Size(bytesRead))
	end := start.Add(maxPayload)
	bytesRead += 2 * maxPayload
	// Send DSACK block for #6 along with SACK for out of
	// order #9 packet.
	start1 := c.IRS.Add(1 + seqnum.Size(bytesRead) + maxPayload)
	end1 := start1.Add(maxPayload)
	c.SendAckWithSACK(seq, bytesRead, []header.SACKBlock{{start, end}, {start1, end1}})

	// Wait for the probe function to finish processing the
	// ACK before the test completes.
	err := <-probeDone
	switch err {
	case failedToDetectDSACK:
		t.Fatalf("RACK DSACK detection failed")
	case invalidDSACKDetected:
		t.Fatalf("RACK DSACK detected when there is no duplicate SACK")
	}
}

// TestRACKDetectDSACKWithOutOfOrderDup tests that DSACK is detected on a
// duplicate of out of order packet.
// See: https://tools.ietf.org/html/rfc2883#section-4.1.3
func TestRACKDetectDSACKWithOutOfOrderDup(t *testing.T) {
	probeDone := make(chan int)
	const ackNumToVerify = 4
	probe := dsackSeenCheckerProbe(t, ackNumToVerify, probeDone)

	c := context.NewWithProbe(t, uint32(mtu), probe)
	defer c.Cleanup()

	numPackets := 10
	e2e.SendAndReceiveWithSACK(t, c, maxPayload, numPackets, true /* enableRACK */)

	// ACK [1-5] packets.
	seq := seqnum.Value(context.TestInitialSequenceNumber).Add(1)
	bytesRead := 5 * maxPayload
	c.SendAck(seq, bytesRead)

	// Send SACK indicating #6 packet is missing and received #7 packet.
	offset := seqnum.Size(bytesRead + maxPayload)
	start := c.IRS.Add(1 + offset)
	end := start.Add(maxPayload)
	c.SendAckWithSACK(seq, bytesRead, []header.SACKBlock{{start, end}})

	// Send SACK with #6 packet is missing and received [7-8] packets.
	end = start.Add(2 * maxPayload)
	c.SendAckWithSACK(seq, bytesRead, []header.SACKBlock{{start, end}})

	// Consider #8 packet is duplicated on the network and send DSACK.
	dsackStart := c.IRS.Add(1 + offset + maxPayload)
	dsackEnd := dsackStart.Add(maxPayload)
	c.SendAckWithSACK(seq, bytesRead, []header.SACKBlock{{dsackStart, dsackEnd}, {start, end}})

	// Wait for the probe function to finish processing the ACK before the
	// test completes.
	err := <-probeDone
	switch err {
	case failedToDetectDSACK:
		t.Fatalf("RACK DSACK detection failed")
	case invalidDSACKDetected:
		t.Fatalf("RACK DSACK detected when there is no duplicate SACK")
	}
}

// TestRACKDetectDSACKSingleDup tests DSACK for a single duplicate subsegment.
// See: https://tools.ietf.org/html/rfc2883#section-4.2.1.
func TestRACKDetectDSACKSingleDup(t *testing.T) {
	probeDone := make(chan int)
	const ackNumToVerify = 4
	probe := dsackSeenCheckerProbe(t, ackNumToVerify, probeDone)

	c := context.NewWithProbe(t, uint32(mtu), probe)
	defer c.Cleanup()

	numPackets := 4
	data := e2e.SendAndReceiveWithSACK(t, c, maxPayload, numPackets, true /* enableRACK */)

	// Send ACK for #1 packet.
	bytesRead := maxPayload
	seq := seqnum.Value(context.TestInitialSequenceNumber).Add(1)
	c.SendAck(seq, bytesRead)

	// Missing [2-3] packets and received #4 packet.
	seq = seqnum.Value(context.TestInitialSequenceNumber).Add(1)
	start := c.IRS.Add(1 + seqnum.Size(3*maxPayload))
	end := start.Add(seqnum.Size(maxPayload))
	c.SendAckWithSACK(seq, bytesRead, []header.SACKBlock{{start, end}})

	// Expect retransmission of #2 packet.
	c.ReceiveAndCheckPacketWithOptions(data, bytesRead, maxPayload, e2e.TSOptionSize)

	// ACK for retransmitted #2 packet.
	bytesRead += maxPayload
	c.SendAckWithSACK(seq, bytesRead, []header.SACKBlock{{start, end}})

	// Simulate receiving delayed subsegment of #2 packet and delayed #3 packet by
	// sending DSACK block for the subsegment.
	dsackStart := c.IRS.Add(1 + seqnum.Size(bytesRead))
	dsackEnd := dsackStart.Add(seqnum.Size(maxPayload / 2))
	c.SendAckWithSACK(seq, numPackets*maxPayload, []header.SACKBlock{{dsackStart, dsackEnd}})

	// Wait for the probe function to finish processing the ACK before the
	// test completes.
	err := <-probeDone
	switch err {
	case failedToDetectDSACK:
		t.Fatalf("RACK DSACK detection failed")
	case invalidDSACKDetected:
		t.Fatalf("RACK DSACK detected when there is no duplicate SACK")
	}

	metricPollFn := func() error {
		tcpStats := c.Stack().Stats().TCP
		stats := []struct {
			stat *tcpip.StatCounter
			name string
			want uint64
		}{
			// Check DSACK was received for a subsegment.
			{tcpStats.SegmentsAckedWithDSACK, "stats.TCP.SegmentsAckedWithDSACK", 1},
		}
		for _, s := range stats {
			if got, want := s.stat.Value(), s.want; got != want {
				return fmt.Errorf("got %s.Value() = %d, want = %d", s.name, got, want)
			}
		}
		return nil
	}

	if err := testutil.Poll(metricPollFn, 1*time.Second); err != nil {
		t.Error(err)
	}
}

// TestRACKDetectDSACKDupWithCumulativeACK tests DSACK for two non-contiguous
// duplicate subsegments covered by the cumulative acknowledgement.
// See: https://tools.ietf.org/html/rfc2883#section-4.2.2.
func TestRACKDetectDSACKDupWithCumulativeACK(t *testing.T) {
	probeDone := make(chan int)
	const ackNumToVerify = 5
	probe := dsackSeenCheckerProbe(t, ackNumToVerify, probeDone)

	c := context.NewWithProbe(t, uint32(mtu), probe)
	defer c.Cleanup()

	numPackets := 6
	data := e2e.SendAndReceiveWithSACK(t, c, maxPayload, numPackets, true /* enableRACK */)

	// Send ACK for #1 packet.
	bytesRead := maxPayload
	seq := seqnum.Value(context.TestInitialSequenceNumber).Add(1)
	c.SendAck(seq, bytesRead)

	// Missing [2-5] packets and received #6 packet.
	seq = seqnum.Value(context.TestInitialSequenceNumber).Add(1)
	start := c.IRS.Add(1 + seqnum.Size(5*maxPayload))
	end := start.Add(seqnum.Size(maxPayload))
	c.SendAckWithSACK(seq, bytesRead, []header.SACKBlock{{start, end}})

	// Expect retransmission of #2 packet.
	c.ReceiveAndCheckPacketWithOptions(data, bytesRead, maxPayload, e2e.TSOptionSize)

	// Received delayed #2 packet.
	bytesRead += maxPayload
	c.SendAckWithSACK(seq, bytesRead, []header.SACKBlock{{start, end}})

	// Received delayed #4 packet.
	start1 := c.IRS.Add(1 + seqnum.Size(3*maxPayload))
	end1 := start1.Add(seqnum.Size(maxPayload))
	c.SendAckWithSACK(seq, bytesRead, []header.SACKBlock{{start1, end1}, {start, end}})

	// Simulate receiving retransmitted subsegment for #2 packet and delayed #3
	// packet by sending DSACK block for #2 packet.
	dsackStart := c.IRS.Add(1 + seqnum.Size(maxPayload))
	dsackEnd := dsackStart.Add(seqnum.Size(maxPayload / 2))
	c.SendAckWithSACK(seq, 4*maxPayload, []header.SACKBlock{{dsackStart, dsackEnd}, {start, end}})

	// Wait for the probe function to finish processing the ACK before the
	// test completes.
	err := <-probeDone
	switch err {
	case failedToDetectDSACK:
		t.Fatalf("RACK DSACK detection failed")
	case invalidDSACKDetected:
		t.Fatalf("RACK DSACK detected when there is no duplicate SACK")
	}
}

// TestRACKDetectDSACKDup tests two non-contiguous duplicate subsegments not
// covered by the cumulative acknowledgement.
// See: https://tools.ietf.org/html/rfc2883#section-4.2.3.
func TestRACKDetectDSACKDup(t *testing.T) {
	probeDone := make(chan int)
	const ackNumToVerify = 5
	probe := dsackSeenCheckerProbe(t, ackNumToVerify, probeDone)

	c := context.NewWithProbe(t, uint32(mtu), probe)
	defer c.Cleanup()

	numPackets := 7
	data := e2e.SendAndReceiveWithSACK(t, c, maxPayload, numPackets, true /* enableRACK */)

	// Send ACK for #1 packet.
	bytesRead := maxPayload
	seq := seqnum.Value(context.TestInitialSequenceNumber).Add(1)
	c.SendAck(seq, bytesRead)

	// Missing [2-6] packets and SACK #7 packet.
	seq = seqnum.Value(context.TestInitialSequenceNumber).Add(1)
	start := c.IRS.Add(1 + seqnum.Size(6*maxPayload))
	end := start.Add(seqnum.Size(maxPayload))
	c.SendAckWithSACK(seq, bytesRead, []header.SACKBlock{{start, end}})

	// Received delayed #3 packet.
	start1 := c.IRS.Add(1 + seqnum.Size(2*maxPayload))
	end1 := start1.Add(seqnum.Size(maxPayload))
	c.SendAckWithSACK(seq, bytesRead, []header.SACKBlock{{start1, end1}, {start, end}})

	// Expect retransmission of #2 packet.
	c.ReceiveAndCheckPacketWithOptions(data, bytesRead, maxPayload, e2e.TSOptionSize)

	// Consider #2 packet has been dropped and SACK #4 packet.
	start2 := c.IRS.Add(1 + seqnum.Size(3*maxPayload))
	end2 := start2.Add(seqnum.Size(maxPayload))
	c.SendAckWithSACK(seq, bytesRead, []header.SACKBlock{{start2, end2}, {start1, end1}, {start, end}})

	// Simulate receiving retransmitted subsegment for #3 packet and delayed #5
	// packet by sending DSACK block for the subsegment.
	dsackStart := c.IRS.Add(1 + seqnum.Size(2*maxPayload))
	dsackEnd := dsackStart.Add(seqnum.Size(maxPayload / 2))
	end1 = end1.Add(seqnum.Size(2 * maxPayload))
	c.SendAckWithSACK(seq, bytesRead, []header.SACKBlock{{dsackStart, dsackEnd}, {start1, end1}})

	// Wait for the probe function to finish processing the ACK before the
	// test completes.
	err := <-probeDone
	switch err {
	case failedToDetectDSACK:
		t.Fatalf("RACK DSACK detection failed")
	case invalidDSACKDetected:
		t.Fatalf("RACK DSACK detected when there is no duplicate SACK")
	}
}

// TestRACKWithInvalidDSACKBlock tests that DSACK is not detected when DSACK
// is not the first SACK block.
func TestRACKWithInvalidDSACKBlock(t *testing.T) {
	probeDone := make(chan struct{})
	const ackNumToVerify = 2
	var n int
	probe := func(state *tcp.TCPEndpointState) {
		// Validate that RACK does not detect DSACK when DSACK block is
		// not the first SACK block.
		n++
		t.Helper()
		if state.Sender.RACKState.DSACKSeen {
			t.Fatalf("RACK DSACK detected when there is no duplicate SACK")
		}

		if n == ackNumToVerify {
			close(probeDone)
		}
	}

	c := context.NewWithProbe(t, uint32(mtu), probe)
	defer c.Cleanup()

	numPackets := 10
	data := e2e.SendAndReceiveWithSACK(t, c, maxPayload, numPackets, true /* enableRACK */)

	// Cumulative ACK for [1-5] packets and SACK for #7 packet (to prevent TLP).
	seq := seqnum.Value(context.TestInitialSequenceNumber).Add(1)
	bytesRead := 5 * maxPayload
	seventhPStart := c.IRS.Add(1 + seqnum.Size(6*maxPayload))
	seventhPEnd := seventhPStart.Add(maxPayload)
	c.SendAckWithSACK(seq, bytesRead, []header.SACKBlock{{seventhPStart, seventhPEnd}})

	// Expect retransmission of #6 packet.
	c.ReceiveAndCheckPacketWithOptions(data, bytesRead, maxPayload, e2e.TSOptionSize)

	// Send DSACK block for #6 packet indicating both
	// initial and retransmitted packet are received and
	// packets [1-7] are received.
	start := c.IRS.Add(1 + seqnum.Size(bytesRead))
	end := start.Add(maxPayload)
	bytesRead += 2 * maxPayload

	// Send DSACK block as second block. The first block is a SACK for #9 packet.
	start1 := c.IRS.Add(1 + seqnum.Size(bytesRead) + maxPayload)
	end1 := start1.Add(maxPayload)
	c.SendAckWithSACK(seq, bytesRead, []header.SACKBlock{{start1, end1}, {start, end}})

	// Wait for the probe function to finish processing the
	// ACK before the test completes.
	<-probeDone
}

func reorderWindowCheckerProbe(numACK int, probeDone chan error) tcp.TCPProbeFunc {
	var n int
	return func(state *tcp.TCPEndpointState) {
		// Validate that RACK detects DSACK.
		n++
		if n < numACK {
			return
		}

		if state.Sender.RACKState.ReoWnd == 0 || state.Sender.RACKState.ReoWnd > state.Sender.RTTState.SRTT {
			probeDone <- fmt.Errorf("got RACKState.ReoWnd: %d, expected it to be greater than 0 and less than %d", state.Sender.RACKState.ReoWnd, state.Sender.RTTState.SRTT)
			return
		}

		if state.Sender.RACKState.ReoWndIncr != 1 {
			probeDone <- fmt.Errorf("got RACKState.ReoWndIncr: %v, want: 1", state.Sender.RACKState.ReoWndIncr)
			return
		}

		if state.Sender.RACKState.ReoWndPersist > 0 {
			probeDone <- fmt.Errorf("got RACKState.ReoWndPersist: %v, want: greater than 0", state.Sender.RACKState.ReoWndPersist)
			return
		}
		probeDone <- nil
	}
}

func TestRACKCheckReorderWindow(t *testing.T) {
	probeDone := make(chan error)
	const ackNumToVerify = 3
	probe := reorderWindowCheckerProbe(ackNumToVerify, probeDone)

	c := context.NewWithProbe(t, uint32(mtu), probe)
	defer c.Cleanup()

	const numPackets = 7
	e2e.SendAndReceiveWithSACK(t, c, maxPayload, numPackets, true /* enableRACK */)

	// Send ACK for #1 packet.
	bytesRead := maxPayload
	seq := seqnum.Value(context.TestInitialSequenceNumber).Add(1)
	c.SendAck(seq, bytesRead)

	// Missing [2-6] packets and SACK #7 packet.
	start := c.IRS.Add(1 + seqnum.Size(6*maxPayload))
	end := start.Add(seqnum.Size(maxPayload))
	c.SendAckWithSACK(seq, bytesRead, []header.SACKBlock{{start, end}})

	// Received delayed packets [2-6] which indicates there is reordering
	// in the connection.
	bytesRead += 6 * maxPayload
	c.SendAck(seq, bytesRead)

	// Wait for the probe function to finish processing the ACK before the
	// test completes.
	if err := <-probeDone; err != nil {
		t.Fatalf("unexpected values for RACK variables: %v", err)
	}
}

func TestRACKWithDuplicateACK(t *testing.T) {
	c := context.New(t, uint32(mtu))
	defer c.Cleanup()

	const numPackets = 4
	data := e2e.SendAndReceiveWithSACK(t, c, maxPayload, numPackets, true /* enableRACK */)

	// Send three duplicate ACKs to trigger fast recovery. The first
	// segment is considered as lost and will be retransmitted after
	// receiving the duplicate ACKs.
	seq := seqnum.Value(context.TestInitialSequenceNumber).Add(1)
	start := c.IRS.Add(1 + seqnum.Size(maxPayload))
	end := start.Add(seqnum.Size(maxPayload))
	for i := 0; i < 3; i++ {
		c.SendAckWithSACK(seq, 0, []header.SACKBlock{{start, end}})
		end = end.Add(seqnum.Size(maxPayload))
	}

	// Receive the retransmitted packet.
	c.ReceiveAndCheckPacketWithOptions(data, 0, maxPayload, e2e.TSOptionSize)

	metricPollFn := func() error {
		tcpStats := c.Stack().Stats().TCP
		stats := []struct {
			stat *tcpip.StatCounter
			name string
			want uint64
		}{
			{tcpStats.FastRetransmit, "stats.TCP.FastRetransmit", 1},
			{tcpStats.SACKRecovery, "stats.TCP.SACKRecovery", 1},
			{tcpStats.FastRecovery, "stats.TCP.FastRecovery", 0},
		}
		for _, s := range stats {
			if got, want := s.stat.Value(), s.want; got != want {
				return fmt.Errorf("got %s.Value() = %d, want = %d", s.name, got, want)
			}
		}
		return nil
	}

	if err := testutil.Poll(metricPollFn, 1*time.Second); err != nil {
		t.Error(err)
	}
}

// TestRACKUpdateSackedOut tests the sacked out field is updated when a SACK
// is received.
func TestRACKUpdateSackedOut(t *testing.T) {
	probeDone := make(chan struct{})
	ackNum := 0
	probe := func(state *tcp.TCPEndpointState) {
		// Validate that the endpoint Sender.SackedOut is what we expect.
		if state.Sender.SackedOut != 2 && ackNum == 0 {
			t.Fatalf("SackedOut got updated to wrong value got: %v want: 2", state.Sender.SackedOut)
		}

		if !state.Sender.FastRecovery.Active && state.Sender.SackedOut != 0 && ackNum == 1 {
			t.Fatalf("SackedOut got updated to wrong value got: %v want: 0", state.Sender.SackedOut)
		}

		if ackNum > 0 {
			close(probeDone)
		}
		ackNum++
	}

	c := context.NewWithProbe(t, uint32(mtu), probe)
	defer c.Cleanup()

	e2e.SendAndReceiveWithSACK(t, c, maxPayload, 8 /* numPackets */, true /* enableRACK */)

	// ACK for [3-5] packets.
	seq := seqnum.Value(context.TestInitialSequenceNumber).Add(1)
	start := c.IRS.Add(seqnum.Size(1 + 3*maxPayload))
	bytesRead := 2 * maxPayload
	end := start.Add(seqnum.Size(bytesRead))
	c.SendAckWithSACK(seq, bytesRead, []header.SACKBlock{{start, end}})

	bytesRead += 3 * maxPayload
	c.SendAck(seq, bytesRead)

	// Wait for the probe function to finish processing the ACK before the
	// test completes.
	<-probeDone
}

// TestRACKWithWindowFull tests that RACK honors the receive window size.
func TestRACKWithWindowFull(t *testing.T) {
	c := context.New(t, uint32(mtu))
	defer c.Cleanup()

	e2e.SetStackSACKPermitted(t, c, true)
	e2e.CreateConnectedWithSACKAndTS(c)

	seq := seqnum.Value(context.TestInitialSequenceNumber).Add(1)
	const numPkts = 10
	data := make([]byte, numPkts*maxPayload)
	for i := range data {
		data[i] = byte(i)
	}

	// Write the data.
	var r bytes.Reader
	r.Reset(data)
	if _, err := c.EP.Write(&r, tcpip.WriteOptions{}); err != nil {
		t.Fatalf("Write failed: %s", err)
	}

	bytesRead := 0
	for i := 0; i < numPkts; i++ {
		c.ReceiveAndCheckPacketWithOptions(data, bytesRead, maxPayload, e2e.TSOptionSize)
		bytesRead += maxPayload
	}

	// Expect retransmission of last packet due to TLP.
	c.ReceiveAndCheckPacketWithOptions(data, (numPkts-1)*maxPayload, maxPayload, e2e.TSOptionSize)

	// SACK for first and last packet.
	start := c.IRS.Add(seqnum.Size(maxPayload))
	end := start.Add(seqnum.Size(maxPayload))
	dsackStart := c.IRS.Add(seqnum.Size(1 + (numPkts-1)*maxPayload))
	dsackEnd := dsackStart.Add(seqnum.Size(maxPayload))
	c.SendAckWithSACK(seq, 2*maxPayload, []header.SACKBlock{{dsackStart, dsackEnd}, {start, end}})

	var info tcpip.TCPInfoOption
	if err := c.EP.GetSockOpt(&info); err != nil {
		t.Fatalf("GetSockOpt failed: %v", err)
	}
	// Wait for RTT to trigger recovery.
	time.Sleep(info.RTT)

	// Expect retransmission of #2 packet.
	c.ReceiveAndCheckPacketWithOptions(data, 2*maxPayload, maxPayload, e2e.TSOptionSize)

	// Send ACK for #2 packet.
	c.SendAck(seq, 3*maxPayload)

	// Expect retransmission of #3 packet.
	c.ReceiveAndCheckPacketWithOptions(data, 3*maxPayload, maxPayload, e2e.TSOptionSize)

	// Send ACK with zero window size.
	c.SendPacket(nil, &context.Headers{
		SrcPort: context.TestPort,
		DstPort: c.Port,
		Flags:   header.TCPFlagAck,
		SeqNum:  seq,
		AckNum:  c.IRS.Add(1 + 4*maxPayload),
		RcvWnd:  0,
	})

	// No packet should be received as the receive window size is zero.
	c.CheckNoPacket("unexpected packet received after userTimeout has expired")
}

func TestMain(m *testing.M) {
	refs.SetLeakMode(refs.LeaksPanic)
	code := m.Run()
	// Allow TCP async work to complete to avoid false reports of leaks.
	// TODO(gvisor.dev/issue/5940): Use fake clock in tests.
	time.Sleep(1 * time.Second)
	refs.DoLeakCheck()
	os.Exit(code)
}

// TestSackedOutAcrossRecovery counts packets beyond one hole before and
// after a cumulative ACK fills it, with and without RACK loss detection.
func TestSackedOutAcrossRecovery(t *testing.T) {
	for _, rack := range []bool{false, true} {
		t.Run(fmt.Sprintf("RACK=%t", rack), func(t *testing.T) {
			states := make(chan tcp.TCPSenderState, 16)
			c := context.NewWithProbe(t, mtu, func(s *tcp.TCPEndpointState) {
				states <- s.Sender
			})
			defer c.Cleanup()
			const packets = 8
			e2e.SendAndReceiveWithSACK(t, c, maxPayload, packets, rack)
			seq := seqnum.Value(context.TestInitialSequenceNumber).Add(1)
			ack := func(cumulative int, blocks []header.SACKBlock, want int) {
				t.Helper()
				c.SendAckWithSACK(seq, cumulative*maxPayload, blocks)
				select {
				case state := <-states:
					t.Logf("cumulative=%d SACK=%v sacked=%d recovery=%t", cumulative, blocks, state.SackedOut, state.FastRecovery.Active)
					if state.SackedOut != want {
						t.Errorf("SackedOut=%d, want %d after ACK of %d packets, SACK %v", state.SackedOut, want, cumulative, blocks)
					}
				case <-time.After(3 * time.Second):
					t.Fatalf("timeout waiting for ACK of %d packets, SACK %v", cumulative, blocks)
				}
			}
			ack(1, nil, 0)
			start := c.IRS.Add(1 + 2*maxPayload)
			for n := 1; n <= 3; n++ {
				ack(1, []header.SACKBlock{{Start: start, End: start.Add(seqnum.Size(n * maxPayload))}}, n)
			}
			ack(packets, nil, 0)
		})
	}
}

// TestSackedOutAfterPMTU counts the packet units covered by the same SACK
// when an ICMP message reduces the path MTU.
func TestSackedOutAfterPMTU(t *testing.T) {
	const (
		linkMTU = 1500
		pathMTU = 1200
		// The sender reserves space for the four SACK blocks negotiated at SYN.
		advertisedMSS = linkMTU - header.IPv4MinimumSize - header.TCPMinimumSize
		sackReserve   = 36
		oldMSS        = advertisedMSS - sackReserve
		newMSS        = pathMTU - header.IPv4MinimumSize - header.TCPMinimumSize - sackReserve
	)
	type sample struct{ mss, sacked int }
	states := make(chan sample, 16)
	c := context.NewWithOpts(t, context.Options{
		EnableV4: true, MTU: linkMTU, Clock: faketime.NewManualClock(),
		Probe: func(s *tcp.TCPEndpointState) { states <- sample{s.Sender.MaxPayloadSize, s.Sender.SackedOut} },
	})
	defer c.Cleanup()
	e2e.SetStackSACKPermitted(t, c, true)
	e2e.SetStackTCPRecovery(t, c, 0)
	c.CreateConnectedWithRawOptions(context.TestInitialSequenceNumber, 30000, -1, []byte{
		header.TCPOptionMSS, 4, byte(advertisedMSS / 256), byte(advertisedMSS % 256),
		header.TCPOptionSACKPermitted, header.TCPOptionSackPermittedLength, header.TCPOptionNOP, header.TCPOptionNOP,
	})
	const packets = 5
	if _, err := c.EP.Write(bytes.NewReader(make([]byte, packets*oldMSS)), tcpip.WriteOptions{}); err != nil {
		t.Fatalf("Write: %s", err)
	}
	var quoted *buffer.View
	for i := 0; i < packets; i++ {
		p := c.GetPacket()
		seg := header.TCP(header.IPv4(p.AsSlice()).Payload())
		if got := len(seg.Payload()); got != oldMSS {
			t.Errorf("original payload = %d, want %d", got, oldMSS)
		}
		if i == 1 {
			quoted = p
			defer quoted.Release()
		} else {
			p.Release()
		}
	}
	start := c.IRS.Add(1 + 2*oldMSS)
	block := []header.SACKBlock{{Start: start, End: start.Add(oldMSS)}}
	var observations, expected []sample
	ack := func(bytes int, blocks []header.SACKBlock, want sample) {
		t.Helper()
		c.SendAckWithSACK(seqnum.Value(context.TestInitialSequenceNumber).Add(1), bytes, blocks)
		select {
		case got := <-states:
			t.Logf("cumulative=%d SACK=%v MSS=%d sacked=%d", bytes, blocks, got.mss, got.sacked)
			observations = append(observations, got)
			expected = append(expected, want)
		case <-time.After(3 * time.Second):
			t.Fatal("no ACK processed")
		}
	}
	ack(oldMSS, nil, sample{oldMSS, 0})
	ack(oldMSS, block, sample{oldMSS, 1})
	mtuField := buffer.NewViewWithData([]byte{0, 0, byte(pathMTU / 256), byte(pathMTU % 256)})
	defer mtuField.Release()
	c.SendICMPPacket(header.ICMPv4DstUnreachable, header.ICMPv4FragmentationNeeded, mtuField, quoted, pathMTU)
	p := c.GetPacket()
	seg := header.TCP(header.IPv4(p.AsSlice()).Payload())
	if got := len(seg.Payload()); got != newMSS {
		t.Errorf("first retransmitted payload = %d, want %d", got, newMSS)
	}
	p.Release()
	ack(oldMSS, block, sample{newMSS, 1})
	ack(packets*oldMSS, nil, sample{newMSS, 0})
	for i, v := range observations {
		if v != expected[i] {
			t.Errorf("sample %d: got %+v, want %+v", i, v, expected[i])
		}
	}
}

func TestSackedOutDisjointBlocks(t *testing.T) {
	for _, rack := range []bool{false, true} {
		t.Run(fmt.Sprintf("RACK=%t", rack), func(t *testing.T) {
			states := make(chan int, 16)
			c := context.NewWithProbe(t, mtu, func(s *tcp.TCPEndpointState) {
				states <- s.Sender.SackedOut
			})
			defer c.Cleanup()
			const packets = 8
			e2e.SendAndReceiveWithSACK(t, c, maxPayload, packets, rack)
			seq := seqnum.Value(context.TestInitialSequenceNumber).Add(1)
			block := func(packet int) header.SACKBlock {
				start := c.IRS.Add(1 + seqnum.Size((packet-1)*maxPayload))
				return header.SACKBlock{Start: start, End: start.Add(maxPayload)}
			}
			ack := func(cum int, blocks []header.SACKBlock) int {
				t.Helper()
				c.SendAckWithSACK(seq, cum*maxPayload, blocks)
				select {
				case n := <-states:
					return n
				case <-time.After(3 * time.Second):
					t.Fatal("no ACK processed")
					return -1
				}
			}
			first := ack(1, nil)
			both := ack(1, []header.SACKBlock{block(6), block(3)})
			repeat := ack(1, []header.SACKBlock{block(3), block(6)})
			last := ack(packets, nil)
			t.Logf("DISJOINT rack=%t first=%d both=%d repeat=%d last=%d", rack, first, both, repeat, last)
			if first != 0 || both != 2 || repeat != 2 || last != 0 {
				t.Errorf("credit=%d,%d,%d,%d, want 0,2,2,0", first, both, repeat, last)
			}
		})
	}
}

func TestSackedOutPartialGSOTrim(t *testing.T) {
	const mss = 1000
	const advertised = mss + 36
	states := make(chan int, 16)
	c := context.NewWithOpts(t, context.Options{EnableV4: true, MTU: 1500, Clock: faketime.NewManualClock(), Probe: func(s *tcp.TCPEndpointState) { states <- s.Sender.SackedOut }})
	defer c.Cleanup()
	c.SetGSOEnabled(true)
	e2e.SetStackSACKPermitted(t, c, true)
	e2e.SetStackTCPRecovery(t, c, int(tcpip.TCPRACKLossDetection))
	c.CreateConnectedWithRawOptions(context.TestInitialSequenceNumber, 30000, -1, []byte{
		header.TCPOptionMSS, 4, byte(advertised / 256), byte(advertised % 256),
		header.TCPOptionSACKPermitted, header.TCPOptionSackPermittedLength, header.TCPOptionNOP, header.TCPOptionNOP,
	})
	if _, err := c.EP.Write(bytes.NewReader(make([]byte, 4*mss)), tcpip.WriteOptions{}); err != nil {
		t.Fatalf("Write: %s", err)
	}
	p := c.GetPacket()
	ip := header.IPv4(p.AsSlice())
	tcpHdr := header.TCP(ip.Payload())
	got := len(tcpHdr.Payload())
	base := seqnum.Value(tcpHdr.SequenceNumber())
	p.Release()
	if got != 4*mss {
		t.Fatalf("first emitted aggregate=%d, want %d", got, 4*mss)
	}
	block := []header.SACKBlock{{Start: base.Add(2 * mss), End: base.Add(3 * mss)}}
	ack := func(bytes int, blocks []header.SACKBlock) int {
		t.Helper()
		c.SendAckWithSACK(seqnum.Value(context.TestInitialSequenceNumber).Add(1), bytes, blocks)
		select {
		case n := <-states:
			return n
		case <-time.After(3 * time.Second):
			t.Fatal("no ACK processed")
			return -1
		}
	}
	first := ack(0, block)
	trim := ack(100, block)
	last := ack(4*mss, nil)
	t.Logf("GSO aggregate=%d first=%d trim=%d last=%d", got, first, trim, last)
	if first != 1 || trim != 1 || last != 0 {
		t.Errorf("credit=%d,%d,%d, want 1,1,0", first, trim, last)
	}
}

// TestSackedOutRetransmissionTiming creates a new SACK hole after a previous
// recovery. The last ACK names three packets together, so only the RACK packet
// threshold, not a series of three duplicate ACKs, shortens its timer.
func TestSackedOutRetransmissionTiming(t *testing.T) {
	clock := faketime.NewManualClock()
	states := make(chan tcp.TCPSenderState, 32)
	c := context.NewWithOpts(t, context.Options{
		EnableV4: true, MTU: mtu, Clock: clock,
		Probe: func(s *tcp.TCPEndpointState) { states <- s.Sender },
	})
	defer c.Cleanup()
	e2e.SetStackSACKPermitted(t, c, true)
	e2e.CreateConnectedWithSACKAndTS(c)
	base := c.IRS.Add(1)
	payload := make([]byte, 16*maxPayload)
	for i := range payload {
		payload[i] = byte(i)
	}
	sent := 0
	write := func(n int) {
		t.Helper()
		if _, err := c.EP.Write(bytes.NewReader(payload[sent*maxPayload:(sent+n)*maxPayload]), tcpip.WriteOptions{}); err != nil {
			t.Fatalf("Write: %s", err)
		}
		for range n {
			c.ReceiveAndCheckPacketWithOptions(payload, sent*maxPayload, maxPayload, e2e.TSOptionSize)
			sent++
		}
	}
	ack := func(cumulative, first, last int) tcp.TCPSenderState {
		t.Helper()
		var blocks []header.SACKBlock
		if first > 0 {
			blocks = []header.SACKBlock{{Start: base.Add(seqnum.Size((first - 1) * maxPayload)), End: base.Add(seqnum.Size(last * maxPayload))}}
		}
		c.SendAckWithSACK(seqnum.Value(context.TestInitialSequenceNumber).Add(1), cumulative*maxPayload, blocks)
		select {
		case s := <-states:
			t.Logf("ack=%d sack=%d-%d sacked=%d cwnd=%d reo=%s reord=%t recovery=%t", cumulative, first, last, s.SackedOut, s.SndCwnd, s.RACKState.ReoWnd, s.RACKState.Reord, s.FastRecovery.Active)
			return s
		case <-time.After(3 * time.Second):
			t.Fatal("timeout waiting for ACK processing")
			return tcp.TCPSenderState{}
		}
	}
	const rtt = 100 * time.Millisecond
	write(1)
	clock.Advance(rtt)
	ack(1, 0, 0)

	// Retransmit packet 2 before filling the first hole. That prevents
	// RACK from treating its later cumulative ACK as delivery reordering.
	write(1)
	clock.Advance(5 * time.Millisecond)
	write(7)
	clock.Advance(rtt)
	for n := 3; n <= 5; n++ {
		ack(1, 3, n)
	}
	c.ReceiveAndCheckPacketWithOptions(payload, maxPayload, maxPayload, e2e.TSOptionSize)
	s := ack(9, 0, 0)
	if s.FastRecovery.Active || s.RACKState.Reord {
		t.Fatalf("the first recovery did not end without reordering: %+v", s.RACKState)
	}
	if s.SndCwnd < 4 {
		t.Fatalf("need 4 packets for second flight, got cwnd=%d", s.SndCwnd)
	}

	// Packet 10 is 5 ms older than 11-13. Their single SACK arrives one
	// RTT later, before MinRTT/4 has elapsed for the older packet.
	write(1)
	clock.Advance(5 * time.Millisecond)
	write(3)
	clock.Advance(rtt)
	s = ack(9, 11, 13)
	p := c.GetPacketNonBlocking()
	immediate := p != nil
	if !immediate {
		clock.Advance(rtt/4 + time.Nanosecond)
		p = c.GetPacket()
	}
	if p == nil {
		t.Fatal("no retransmission by the reordering deadline")
	}
	defer p.Release()
	seg := header.TCP(header.IPv4(p.AsSlice()).Payload())
	seq := seqnum.Value(seg.SequenceNumber())
	want := base.Add(9 * maxPayload)
	if seq != want {
		t.Fatalf("retransmission starts at %d, want %d", seq, want)
	}
	t.Logf("SECOND_HOLE immediate=%t seq=packet10 count=%d reownd=%s", immediate, s.SackedOut, s.RACKState.ReoWnd)
	if !immediate {
		t.Error("three SACKed packets did not trigger retransmission at the ACK")
	}
}
