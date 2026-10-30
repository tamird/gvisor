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
	t.Skipf("Skipping this test as reorder detection does not consider DSACK.")

	var n int
	const ackNumToVerify = 2
	probeDone := make(chan struct{})
	probe := func(state *tcp.TCPEndpointState) {
		gotSeq := state.Sender.RACKState.FACK
		wantSeq := state.Sender.SndNxt
		// FACK should be updated to the highest ending sequence number of the
		// segment acknowledged most recently.
		if !gotSeq.LessThanEq(wantSeq) || gotSeq.LessThan(wantSeq) {
			t.Fatalf("RACK FACK failed to update, got: %v, but want: %v", gotSeq, wantSeq)
		}

		n++
		if n < ackNumToVerify {
			if state.Sender.RACKState.Reord {
				t.Fatalf("RACK reorder detected when there is no reordering")
			}
			return
		}

		if state.Sender.RACKState.Reord == false {
			t.Fatalf("RACK reorder detection failed")
		}
		close(probeDone)
	}

	c := context.NewWithProbe(t, uint32(mtu), probe)
	defer c.Cleanup()

	e2e.SetStackSACKPermitted(t, c, true)
	e2e.CreateConnectedWithSACKAndTS(c)
	data := make([]byte, ackNumToVerify*maxPayload)
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
	for i := 0; i < ackNumToVerify; i++ {
		c.ReceiveAndCheckPacketWithOptions(data, bytesRead, maxPayload, e2e.TSOptionSize)
		bytesRead += maxPayload
	}

	start := c.IRS.Add(maxPayload + 1)
	end := start.Add(maxPayload)
	seq := seqnum.Value(context.TestInitialSequenceNumber).Add(1)
	c.SendAckWithSACK(seq, 0, []header.SACKBlock{{start, end}})
	c.SendAck(seq, bytesRead)

	// Wait for the probe function to finish processing the ACK before the
	// test completes.
	<-probeDone
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

// recoverAfterRTOTailLoss sends ten segments and acknowledges none of them, so
// the PTO sends a TLP and the RTO retransmits #1. Acknowledging #1 makes RACK
// mark #2-#10 lost and enter recovery with cwnd = ssthresh + 3 = 5. With GSO
// the unacknowledged data is one segment that recovery splits, so the marks
// must survive the split. It returns RACK's RTT sample from that ACK.
func recoverAfterRTOTailLoss(t *testing.T, gso bool) (*context.Context, []byte, time.Duration) {
	t.Helper()
	rtts := make(chan time.Duration, 1)
	c := context.NewWithProbe(t, uint32(mtu), func(state *tcp.TCPEndpointState) {
		select {
		case rtts <- state.Sender.RACKState.RTT:
		default:
		}
	})
	t.Cleanup(c.Cleanup)
	c.SetGVisorGSOEnabled(gso)

	const numPackets = 10
	data := e2e.SendAndReceiveWithSACK(t, c, maxPayload, numPackets, true /* enableRACK */)
	c.GetPacket().Release()
	c.ReceiveAndCheckPacketWithOptions(data, 0, maxPayload, e2e.TSOptionSize)

	for len(rtts) > 0 {
		<-rtts
	}
	c.SendAck(seqnum.Value(context.TestInitialSequenceNumber).Add(1), maxPayload)
	// #2 is fast-retransmitted and counted twice in pipe (RFC 6675 SetPipe
	// rule (b)), which leaves room for #3-#5.
	for i := 1; i < 5; i++ {
		c.ReceiveAndCheckPacketWithOptions(data, i*maxPayload, maxPayload, e2e.TSOptionSize)
	}
	c.CheckNoPacketTimeout("unexpected packet beyond cwnd", 100*time.Millisecond)
	if got := c.Stack().Stats().TCP.Timeouts.Value(); got != 1 {
		t.Errorf("got stats.TCP.Timeouts.Value() = %d, want = 1", got)
	}

	var rtt time.Duration
	select {
	case rtt = <-rtts:
	case <-time.After(5 * time.Second):
		t.Fatalf("no probe for the ACK of #1")
	}
	return c, data, rtt
}

// TestRACKRecoveryAfterRTOTailLoss tests that segments RACK marks lost are not
// counted as in flight, so recovery retransmits them without waiting for
// another RTO.
func TestRACKRecoveryAfterRTOTailLoss(t *testing.T) {
	for _, gso := range []bool{false, true} {
		t.Run(fmt.Sprintf("gso=%t", gso), func(t *testing.T) {
			recoverAfterRTOTailLoss(t, gso)
		})
	}
}

// TestRACKLossDuringRecovery tests that segments RACK marks lost on an ACK
// during recovery stop counting as in flight before that ACK retransmits.
func TestRACKLossDuringRecovery(t *testing.T) {
	for _, tc := range []struct {
		name string
		gso  bool
		// sacked is the range of retransmitted segments the ACK SACKs, as
		// [first, last) zero-based segment indexes.
		sacked [2]int
		want   []int
	}{
		{
			// RACK marks #2-#4 lost again. Pipe is 1 (#2 under rule (b)), so
			// #2-#4 are retransmitted and #6 sent.
			name:   "sack #5 gso=false",
			sacked: [2]int{4, 5},
			want:   []int{1, 2, 3, 5},
		},
		{
			// RACK marks #2 lost again, which the scoreboard alone does not
			// (one SACKed segment above it). Pipe is 3, so #2 and #6 are sent.
			name:   "sack #3 gso=false",
			sacked: [2]int{2, 3},
			want:   []int{1, 5},
		},
		{
			// With GSO, #3-#5 were retransmitted as one segment.
			name:   "sack #3 gso=true",
			gso:    true,
			sacked: [2]int{2, 3},
			want:   []int{1, 5},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c, data, rtt := recoverAfterRTOTailLoss(t, tc.gso)

			// RACK ignores an RTT sample of a retransmission that is below its
			// minimum RTT.
			time.Sleep(rtt + time.Millisecond)

			start := c.IRS.Add(seqnum.Size(1 + tc.sacked[0]*maxPayload))
			end := c.IRS.Add(seqnum.Size(1 + tc.sacked[1]*maxPayload))
			c.SendAckWithSACK(seqnum.Value(context.TestInitialSequenceNumber).Add(1), maxPayload, []header.SACKBlock{{start, end}})
			for _, i := range tc.want {
				c.ReceiveAndCheckPacketWithOptions(data, i*maxPayload, maxPayload, e2e.TSOptionSize)
			}
			c.CheckNoPacketTimeout("unexpected packet beyond cwnd", 100*time.Millisecond)
			if got := c.Stack().Stats().TCP.Timeouts.Value(); got != 1 {
				t.Errorf("got stats.TCP.Timeouts.Value() = %d, want = 1", got)
			}
		})
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
	// Wait for RTT to trigger recovery. RACK marks #3-#9 lost and they are
	// retransmitted up to cwnd: #3-#8.
	time.Sleep(info.RTT)
	for i := 2; i < numPkts-2; i++ {
		c.ReceiveAndCheckPacketWithOptions(data, i*maxPayload, maxPayload, e2e.TSOptionSize)
	}

	// ACK #3 with a zero window. cwnd now has room for #9, the window does not.
	c.SendPacket(nil, &context.Headers{
		SrcPort: context.TestPort,
		DstPort: c.Port,
		Flags:   header.TCPFlagAck,
		SeqNum:  seq,
		AckNum:  c.IRS.Add(1 + 3*maxPayload),
		RcvWnd:  0,
	})

	// No packet should be received as the receive window size is zero.
	c.CheckNoPacket("unexpected packet received with a zero receive window")
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

// TestRACKRepeatedRepairHistory observes the captured cumulative ACK pair in
// a live CUBIC sender. Its prehistory is synthetic; packet counts decide
// whether the generated connection reproduced the observed three earlier losses.
func TestRACKRepeatedRepairHistory(t *testing.T) {
	const mss = 1420
	const tail = 468568
	const rtt = 100 * time.Millisecond
	const advertisedMSS = mss + 36
	clock := faketime.NewManualClock()
	states := make(chan tcp.TCPSenderState, 128)
	c := context.NewWithOpts(t, context.Options{MTU: 1500, EnableV4: true, Clock: clock,
		Probe: func(s *tcp.TCPEndpointState) { states <- s.Sender }})
	defer c.Cleanup()
	e2e.EnableCUBIC(t, c)
	e2e.SetStackSACKPermitted(t, c, true)
	opts := []byte{header.TCPOptionMSS, 4, advertisedMSS >> 8, advertisedMSS & 0xff, header.TCPOptionSACKPermitted, 2, header.TCPOptionWS, 3, 4, header.TCPOptionNOP, header.TCPOptionNOP, header.TCPOptionNOP}
	if len(opts)%4 != 0 {
		t.Fatalf("unaligned SYN options: %d bytes", len(opts))
	}
	c.CreateConnectedWithRawOptions(context.TestInitialSequenceNumber, 30000, -1, opts)
	c.EP.SocketOptions().SetDelayOption(false)
	begin := c.IRS.Add(1)
	peer := seqnum.Value(context.TestInitialSequenceNumber).Add(1)
	var now time.Duration
	advance := func(d time.Duration) { clock.Advance(d); now += d }
	write := func(n int) {
		t.Helper()
		got, err := c.EP.Write(bytes.NewReader(make([]byte, n)), tcpip.WriteOptions{})
		if err != nil || got != int64(n) {
			t.Fatalf("write %d = (%d,%v)", n, got, err)
		}
	}
	type span struct{ start, end int }
	type wire struct {
		span
		at time.Duration
	}
	var warm int
	recv := func(timeout time.Duration) (wire, bool) {
		t.Helper()
		p := c.GetPacketNonBlocking()
		if p == nil && timeout > 0 {
			p = c.GetPacketWithTimeout(timeout)
		}
		if p == nil {
			return wire{}, false
		}
		h := header.TCP(header.IPv4(p.AsSlice()).Payload())
		x := int(uint32(h.SequenceNumber())-uint32(begin)) - warm
		end := x + len(h.Payload())
		p.Release()
		return wire{span{x, end}, now}, true
	}
	ack := func(n, w int, blocks []span) tcp.TCPSenderState {
		t.Helper()
		var sb []header.SACKBlock
		for _, b := range blocks {
			sb = append(sb, header.SACKBlock{Start: begin.Add(seqnum.Size(warm + b.start)), End: begin.Add(seqnum.Size(warm + b.end))})
		}
		opt := make([]byte, 40)
		l := 0
		if len(sb) > 0 {
			l += header.EncodeNOP(opt[l:])
			l += header.EncodeNOP(opt[l:])
			l += header.EncodeSACKBlocks(sb, opt[l:])
		}
		c.SendPacket(nil, &context.Headers{SrcPort: context.TestPort, DstPort: c.Port, Flags: header.TCPFlagAck, SeqNum: peer, AckNum: begin.Add(seqnum.Size(n)), RcvWnd: seqnum.Size(w), TCPOpts: opt[:l]})
		select {
		case st := <-states:
			if st.SndUna != begin.Add(seqnum.Size(n)) {
				t.Fatalf("ACK %d yielded SndUna %v", n, st.SndUna)
			}
			return st
		case <-time.After(5 * time.Second):
			t.Fatal("no state after ACK")
			return tcp.TCPSenderState{}
		}
	}
	cwnd := 10
	for cwnd < 335 {
		// No receive-window-limited growth or unobserved packets.
		count := cwnd
		if count > 335-cwnd {
			count = 335 - cwnd
		}
		n := count * mss
		write(n)
		got := 0
		for got < n {
			p, ok := recv(5 * time.Second)
			if !ok || p.end == p.start {
				t.Fatalf("warm bytes %d/%d", got, n)
			}
			got += p.end - p.start
		}
		warm += n
		advance(rtt)
		st := ack(warm, 64000, nil)
		if st.SndCwnd <= cwnd {
			t.Fatalf("warm cwnd stalled at %d", st.SndCwnd)
		}
		cwnd = st.SndCwnd
	}
	// Split writes at each observed low/SACK boundary, so the short low
	// range is a genuine 108-byte wire transmission. Flight beyond the
	// highest SACK contains 61,276 unacknowledged bytes.
	fixed := []span{{344760, 407292}, {341920, 343340}, {74164, 75584}}
	focus := []span{{0, 1420}, {1420, 1528}, {1528, 2948}}
	var initial []wire
	pos := 0
	for _, end := range []int{1420, 1528, 2948, 74164, 75584, 341920, 343340, 344760, 407292, tail} {
		write(end - pos)
		for pos < end {
			p, ok := recv(5 * time.Second)
			if !ok || p.start != pos || p.end <= p.start || p.end > end {
				t.Fatalf("original offset %d: %+v present=%t end=%d", pos, p, ok, end)
			}
			initial = append(initial, p)
			pos = p.end
		}
	}
	for i, b := range focus {
		if initial[i].span != b {
			t.Fatalf("original[%d]=%v want %v", i, initial[i], b)
		}
	}
	t.Logf("HISTORY fixture base=%d warm=%d mss=%d initial_packets=%d cwnd=%d tail=%d", uint32(begin)+uint32(warm), warm, mss, len(initial), cwnd, tail)
	tx := map[span]int{}
	for _, p := range initial {
		tx[p.span]++
	}
	counts := func() [3]int { return [3]int{tx[focus[0]], tx[focus[1]], tx[focus[2]]} }
	drain := func(wait time.Duration) []wire {
		var batch []wire
		for p, ok := recv(wait); ok; p, ok = recv(0) {
			if p.end > p.start {
				tx[p.span]++
				batch = append(batch, p)
			}
		}
		return batch
	}
	printState := func(label string, n int, st tcp.TCPSenderState, batch []wire) {
		var lows []span
		for _, p := range batch {
			if p.start < 2948 {
				lows = append(lows, p.span)
			}
		}
		t.Logf("HISTORY %s t_us=%d ack=%d timeout=%d cwnd=%d pipe=%d sacked=%d high=%d sndnxt=%d count=%v low_tx=%v batch=%d", label, now.Microseconds(), n, c.Stack().Stats().TCP.Timeouts.Value(), st.SndCwnd, st.Outstanding, st.SackedOut, int32(st.FastRecovery.HighRxt-begin)-int32(warm), int32(st.SndNxt-begin)-int32(warm), counts(), lows, len(batch))
	}
	advance(rtt)
	st := ack(warm, 29286, fixed)
	batch := drain(50 * time.Millisecond)
	printState("initial_sack", 0, st, batch)
	// Deliver at most one higher repair per report. This gives RACK real
	// proof of a transmission later than the lost lower repairs. The
	// captured peer's final SACK ranges stay retained throughout.
	var extra span
	for wave := 0; wave < 10 && (counts()[0] < 3 || counts()[1] < 3 || counts()[2] < 3); wave++ {
		var next span
		for _, p := range batch {
			if p.start >= 2948 && p.end <= fixed[2].start && p.span != extra {
				next = p.span
			}
		}
		if next != (span{}) {
			extra = next
		}
		blocks := fixed
		if extra != (span{}) {
			blocks = append([]span{extra}, fixed...)
		}
		advance(rtt)
		drain(0) // Timer-driven output is accounted separately from this ACK.
		st = ack(warm, 29286, blocks)
		batch = drain(50 * time.Millisecond)
		printState("prehistory", 0, st, batch)
	}
	// Now suspend lower delivery until a real RTO has retransmitted the
	// first low range. This resets SACK credit, independently of RACK's
	// per-transmission history. No actual timer firing is inferred from time.
	rtoBefore := c.Stack().Stats().TCP.Timeouts.Value()
	for i := 0; i < 3000 && c.Stack().Stats().TCP.Timeouts.Value() == rtoBefore; i++ {
		advance(10 * time.Millisecond)
		batch = drain(0)
	}
	foundFirst := false
	for _, p := range batch {
		if p.span == focus[0] {
			foundFirst = true
		}
	}
	if !foundFirst || c.Stack().Stats().TCP.Timeouts.Value() != rtoBefore+1 {
		t.Fatalf("no RTO repair of first full range: timeouts=%d batch=%+v", c.Stack().Stats().TCP.Timeouts.Value(), batch)
	}
	before := counts()
	t.Logf("HISTORY final_rto t_us=%d count=%v", now.Microseconds(), before)
	// Queue unsent application data for the gated period. The actual
	// remote ACK opens to the original observed 1,024,000-byte window.
	write(16 * mss)
	drain(0)
	advance(rtt)
	prior := drain(0)
	st = ack(warm+1420, 64000, fixed)
	batch = drain(50 * time.Millisecond)
	printState("ack_full", 1420, st, batch)
	t.Logf("COPY first_batch=%v", batch)
	shortReceived := false
	thirdAlreadySent := false
	for _, p := range append(prior, batch...) {
		if p.span == focus[1] {
			shortReceived = true
		}
		if p.span == focus[2] {
			thirdAlreadySent = true
		}
	}
	if !shortReceived {
		t.Logf("HISTORY outcome divergent_no_short pre=%v", before)
		return
	}
	advance(rtt)
	early := drain(0)
	st = ack(warm+1528, 64000, fixed)
	batch = drain(50 * time.Millisecond)
	printState("ack_short", 1528, st, batch)
	t.Logf("COPY second_batch=%v", batch)
	if thirdAlreadySent {
		t.Logf("HISTORY outcome third_already_sent_on_full_ack pre=%v post=%v fourth_prefix=%t", before, counts(), before == [3]int{4, 3, 3})
		return
	}
	all := append(early, batch...)
	at := now
	thirdPresent := func() bool {
		for _, p := range all {
			if p.span == focus[2] {
				return true
			}
		}
		return false
	}
	t0 := c.Stack().Stats().TCP.Timeouts.Value()
	for i := 0; !thirdPresent() && i < 3000; i++ {
		advance(10 * time.Millisecond)
		all = append(all, drain(0)...)
	}
	if !thirdPresent() {
		t.Logf("HISTORY outcome no_third_within_bound pre=%v", before)
		return
	}
	t.Logf("HISTORY outcome ack_pair pre=%v gap_us=%d new_timeouts=%d fourth_prefix=%t", before, (now - at).Microseconds(), c.Stack().Stats().TCP.Timeouts.Value()-t0, before == [3]int{4, 3, 3})
}

// TestRACKRetainedSACKWithMissingTail records which CUBIC repairs leave on an
// ACK and which require a timeout. The peer retains three original packets,
// drops the rest and discards early repairs until the first RTO. After that
// every emitted packet is delivered and acknowledged after a 100 ms RTT.
func TestRACKRetainedSACKWithMissingTail(t *testing.T) {
	const rtt = 100 * time.Millisecond
	clock := faketime.NewManualClock()
	states := make(chan tcp.TCPSenderState, 32)
	c := context.NewWithOpts(t, context.Options{
		MTU: mtu, EnableV4: true, Clock: clock,
		Probe: func(s *tcp.TCPEndpointState) { states <- s.Sender },
	})
	defer c.Cleanup()
	e2e.EnableCUBIC(t, c)
	e2e.SetStackSACKPermitted(t, c, true)
	e2e.CreateConnectedWithSACKAndTS(c)
	write := func(n int) {
		t.Helper()
		if got, err := c.EP.Write(bytes.NewReader(make([]byte, n)), tcpip.WriteOptions{}); err != nil || got != int64(n) {
			t.Fatalf("Write(%d) = (%d, %v)", n, got, err)
		}
	}
	const warm = 8
	seq := seqnum.Value(context.TestInitialSequenceNumber).Add(1)
	first := c.IRS.Add(1 + warm*maxPayload)
	var elapsed time.Duration
	advance := func(d time.Duration) { clock.Advance(d); elapsed += d }
	ack := func(packets int, blocks []header.SACKBlock) tcp.TCPSenderState {
		t.Helper()
		c.SendAckWithSACK(seq, (warm+packets)*maxPayload, blocks)
		select {
		case st := <-states:
			if want := first.Add(seqnum.Size(packets * maxPayload)); st.SndUna != want {
				t.Fatalf("sender SndUna = %v, want %v", st.SndUna, want)
			}
			return st
		case <-time.After(5 * time.Second):
			t.Fatal("ACK was not processed")
			return tcp.TCPSenderState{}
		}
	}
	write(warm * maxPayload)
	for i := 0; i < warm; i++ {
		c.ReceiveAndCheckPacketWithOptions(make([]byte, warm*maxPayload), i*maxPayload, maxPayload, e2e.TSOptionSize)
	}
	advance(rtt)
	st := ack(0, nil)
	if st.RTTState.SRTT <= 0 {
		t.Fatal("no positive trained RTT")
	}

	// The test context emits one MSS per packet. Read complete egress while
	// the manual clock stays frozen, without consuming any sender timeout.
	read := func() []int {
		t.Helper()
		var tx []int
		for {
			p := c.GetPacketWithTimeout(50 * time.Millisecond)
			if p == nil {
				return tx
			}
			h := header.TCP(header.IPv4(p.AsSlice()).Payload())
			n := len(h.Payload())
			offset := uint32(h.SequenceNumber()) - uint32(first)
			p.Release()
			if n == 0 {
				continue
			}
			if n != maxPayload || offset%maxPayload != 0 {
				t.Fatalf("unexpected egress offset %d, bytes %d", offset, n)
			}
			tx = append(tx, int(offset)/maxPayload+1)
		}
	}
	write((st.SndCwnd + 12) * maxPayload)
	initial := read()
	if len(initial) < 10 {
		t.Fatalf("only %d original packets: %v", len(initial), initial)
	}
	for i, n := range initial {
		if n != i+1 {
			t.Fatalf("original sequence %v", initial)
		}
	}
	t.Logf("PIPE initial=%v queued_more=12 srtt=%s", initial, st.RTTState.SRTT)
	// Originals 4--6 arrive, leaving at least four unsacked originals above
	// the highest SACK as well as holes below it. Hold all subsequent
	// repairs until a real RTO retransmits packet 1.
	block := header.SACKBlock{Start: first.Add(3 * maxPayload), End: first.Add(6 * maxPayload)}
	advance(rtt)
	st = ack(0, []header.SACKBlock{block})
	early := read()
	t.Logf("PIPE before_timeout t=%s sacked=%d out=%d cwnd=%d repairs=%v", elapsed, st.SackedOut, st.Outstanding, st.SndCwnd, early)
	if st.SackedOut != 3 {
		t.Fatalf("pre-timeout credited %d, want three high originals", st.SackedOut)
	}
	timeoutCount := func() uint64 { return c.Stack().Stats().TCP.Timeouts.Value() }
	// Advance in small increments and leave the clock frozen at the first
	// real timeout. Discard earlier PTO probes with the early repairs.
	var afterRTO []int
	for i := 0; i < 500 && timeoutCount() == 0; i++ {
		advance(10 * time.Millisecond)
		if timeoutCount() != 0 {
			afterRTO = read()
		} else {
			for p := c.GetPacketNonBlocking(); p != nil; p = c.GetPacketNonBlocking() {
				p.Release()
			}
		}
	}
	if timeoutCount() != 1 || len(afterRTO) == 0 || afterRTO[0] != 1 {
		t.Fatalf("first timeout = %d, emitted %v", timeoutCount(), afterRTO)
	}
	t.Logf("PIPE first_timeout t=%s packets=%v", elapsed, afterRTO)
	delivered := map[int]bool{4: true, 5: true, 6: true}
	for _, n := range afterRTO {
		delivered[n] = true
	}
	// Stop after three advancing ACKs. If no repair is sent on an ACK,
	// advance the manual clock until the next *observed* sender timeout.
	cumulative := 0
	for round := 1; round <= 3; round++ {
		for delivered[cumulative+1] {
			cumulative++
		}
		var sacks []header.SACKBlock
		for n := cumulative + 1; n <= len(initial)+12; n++ {
			if !delivered[n] {
				continue
			}
			lo := n
			for delivered[n+1] {
				n++
			}
			sacks = append(sacks, header.SACKBlock{Start: first.Add(seqnum.Size((lo - 1) * maxPayload)), End: first.Add(seqnum.Size(n * maxPayload))})
		}
		advance(rtt)
		inTransit := read()
		if len(inTransit) > 0 {
			t.Logf("PIPE in_transit t=%s timeouts=%d packets=%v", elapsed, timeoutCount(), inTransit)
		}
		st = ack(cumulative, sacks)
		sent := read()
		t.Logf("PIPE ack=%d t=%s timeouts=%d active=%t cwnd=%d out=%d sacked=%d high_rxt=%d sndnxt=%d packets=%v", cumulative, elapsed, timeoutCount(), st.FastRecovery.Active, st.SndCwnd, st.Outstanding, st.SackedOut, int32(st.FastRecovery.HighRxt-first), int32(st.SndNxt-first), sent)
		if round == 3 {
			break
		}
		// Several packets can be repaired in one ACK round. If it made no
		// cumulative progress, distinguish a later timer from ACK-driven work.
		for _, n := range append(inTransit, sent...) {
			delivered[n] = true
		}
		for i := 0; !delivered[cumulative+1] && i < 500; i++ {
			advance(10 * time.Millisecond)
			var tx []int
			for p := c.GetPacketNonBlocking(); p != nil; p = c.GetPacketNonBlocking() {
				h := header.TCP(header.IPv4(p.AsSlice()).Payload())
				if len(h.Payload()) > 0 {
					tx = append(tx, int(uint32(h.SequenceNumber())-uint32(first))/maxPayload+1)
				}
				p.Release()
			}
			if len(tx) != 0 {
				t.Logf("PIPE later t=%s timeouts=%d packets=%v", elapsed, timeoutCount(), tx)
				for _, n := range tx {
					delivered[n] = true
				}
			}
		}
		if !delivered[cumulative+1] {
			t.Fatalf("no next repair for packet %d", cumulative+1)
		}
	}
}
