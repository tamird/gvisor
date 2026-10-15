// Copyright 2026 The gVisor Authors.
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

// Package tcp_acktiming_test verifies that TCP RTT measurements are anchored to
// the time an ACK arrived at the stack (its ingress timestamp, segment.rcvdTime)
// rather than the time the stack got around to processing it.
//
// These are regression tests for the gvisor#9707 / gvisor#9778 family: when the
// sending endpoint is busy (e.g. the application holds the endpoint lock during
// a Write that synchronously flushes a window of segments, paying real
// per-packet cost such as WireGuard encryption), inbound ACKs sit unprocessed in
// the endpoint's segment queue. If an RTT is measured against the processing-time
// clock instead of the ACK's ingress time, it is inflated by that internal
// delay. For RACK this corrupts RACK.RTT/minRTT and the reorder window and
// triggers spurious loss detection; for the main-path RTT it inflates SRTT/RTO.
package tcp_acktiming_test

import (
	"bytes"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/refs"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/faketime"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/seqnum"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp/test/e2e"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp/testing/context"
)

const (
	maxPayload = 10
	mtu        = header.TCPMinimumSize + header.IPv4MinimumSize + 40 + maxPayload
)

func TestMain(m *testing.M) {
	refs.SetLeakMode(refs.NoLeakChecking)
	m.Run()
}

// agedACKResult holds the sender state captured by the probe when the aged ACK
// was processed.
type agedACKResult struct {
	rackRTT time.Duration
	srtt    time.Duration
	rto     time.Duration
}

type tcpStateSample struct {
	sndUna  seqnum.Value
	srtt    time.Duration
	rackRTT time.Duration
	rto     time.Duration
}

// runAgedACK establishes a connection, sends one segment, and then processes its
// ACK after an internal processing delay of processHold (created by holding the
// endpoint lock via StopWork while virtual time advances). The ACK genuinely
// arrives trueRTT after transmission. useTS selects whether the connection
// negotiates TCP timestamps (which selects the timestamp vs non-timestamp RTT
// path in the sender). It returns the sender RTT state observed when the aged
// ACK is processed.
func runAgedACK(t *testing.T, useTS bool, trueRTT, processHold time.Duration) agedACKResult {
	t.Helper()

	clock := faketime.NewManualClock()

	stateCh := make(chan tcpStateSample, 256)
	probe := func(state *tcp.TCPEndpointState) {
		// Non-blocking: never stall the stack's processing goroutine.
		select {
		case stateCh <- tcpStateSample{
			sndUna:  state.Sender.SndUna,
			srtt:    state.Sender.RTTState.SRTT,
			rackRTT: state.Sender.RACKState.RTT,
			rto:     state.Sender.RTO,
		}:
		default:
		}
	}

	c := context.NewWithOpts(t, context.Options{
		EnableV4: true,
		EnableV6: true,
		MTU:      mtu,
		Clock:    clock,
		Probe:    probe,
	})
	defer c.Cleanup()

	e2e.SetStackSACKPermitted(t, c, true)
	// Always negotiate timestamps so we can choose, per test, whether the
	// injected ACK carries a TSEcr (which selects the timestamp RTT path,
	// snd.go) or not (the non-timestamp path). rep is the peer (link) side.
	rep := e2e.CreateConnectedWithSACKAndTS(c)

	tcpEP, ok := c.EP.(interface {
		StopWork()
		ResumeWork()
	})
	if !ok {
		t.Fatalf("endpoint %T does not expose StopWork/ResumeWork", c.EP)
	}

	data := make([]byte, maxPayload)
	for i := range data {
		data[i] = byte(i)
	}

	// Send one segment and read it off the link, capturing the data segment's
	// TSVal so the ACK below can echo it back as TSEcr (selecting the timestamp
	// RTT path).
	var r bytes.Reader
	r.Reset(data)
	if _, err := c.EP.Write(&r, tcpip.WriteOptions{}); err != nil {
		t.Fatalf("Write failed: %s", err)
	}
	dataPkt := c.GetPacket()
	dataTCP := header.TCP(header.IPv4(dataPkt.AsSlice()).Payload())
	dataTSVal := dataTCP.ParsedOptions().TSVal
	dataPkt.Release()
	if useTS && dataTSVal == 0 {
		t.Fatal("data segment carried no TSVal; cannot exercise the timestamp RTT path")
	}

	// The ACK genuinely arrives one RTT after transmission.
	clock.Advance(trueRTT)

	// Drain probe samples produced so far so we observe the post-ACK state.
	for len(stateCh) > 0 {
		<-stateCh
	}

	// Create the internal processing delay: hold the endpoint lock so the
	// injected ACK cannot be processed, advance virtual time while it sits in
	// the segment queue, then release the lock so it is processed against the
	// advanced clock. With the fix, the RTT is computed from the ACK's ingress
	// time (rcvdTime) and is not inflated.
	//
	// NB: StopWork takes the endpoint lock directly; it models the processing
	// delay (segment stamped at SendAck time, processed after the clock
	// advanced), not the exact LockUser/UnlockUser handoff of the real bug.
	rep.NextSeqNum = seqnum.Value(context.TestInitialSequenceNumber).Add(1)
	rep.AckNum = c.IRS.Add(1 + seqnum.Size(len(data)))
	rep.Flags = header.TCPFlagAck
	tcpEP.StopWork()
	if useTS {
		// Echo the data segment's TSVal as TSEcr so the sender takes the
		// timestamp RTT path (s.ep.elapsed(rcvdSeg.rcvdTime, TSEcr)).
		rep.RecentTS = dataTSVal
		rep.SendPacketWithTS(nil, rep.TSVal+1)
	} else {
		c.SendAck(seqnum.Value(context.TestInitialSequenceNumber).Add(1), len(data))
	}
	// Keep processHold below the (3s test) RTO so no retransmit timer fires
	// while we hold the lock.
	clock.Advance(processHold)
	tcpEP.ResumeWork()

	// Wait for the probe to report the state after the aged ACK is processed.
	deadline := time.After(5 * time.Second)
	for {
		select {
		case st := <-stateCh:
			// The ACK acknowledges our one segment, advancing SndUna. Once that
			// has happened the RTT state reflects this ACK.
			if st.sndUna == rep.AckNum {
				return agedACKResult{
					rackRTT: st.rackRTT,
					srtt:    st.srtt,
					rto:     st.rto,
				}
			}
		case <-deadline:
			t.Fatal("timed out waiting for the aged ACK to be processed")
		}
	}
}

// TestRACKRTTNotInflatedByProcessingDelay covers rackControl.update (RACK RTT).
func TestRACKRTTNotInflatedByProcessingDelay(t *testing.T) {
	const (
		trueRTT     = 50 * time.Millisecond
		processHold = 250 * time.Millisecond
	)
	got := runAgedACK(t, true /* useTS */, trueRTT, processHold)
	t.Logf("true RTT=%v hold=%v: RACK RTT=%v SRTT=%v", trueRTT, processHold, got.rackRTT, got.srtt)
	if got.rackRTT > trueRTT+processHold/2 {
		t.Errorf("RACK RTT inflated by internal processing delay: got %v, want <= %v; "+
			"RTT measured against processing time instead of the ACK's ingress time (rcvdTime)",
			got.rackRTT, trueRTT+processHold/2)
	}
}

// TestSRTTNotInflatedByProcessingDelayTS covers the timestamp-based main-path
// RTT in sender.handleRcvdSegment.
func TestSRTTNotInflatedByProcessingDelayTS(t *testing.T) {
	const (
		trueRTT     = 50 * time.Millisecond
		processHold = 250 * time.Millisecond
	)
	got := runAgedACK(t, true /* useTS */, trueRTT, processHold)
	t.Logf("true RTT=%v hold=%v: SRTT=%v RTO=%v", trueRTT, processHold, got.srtt, got.rto)
	// SRTT is the first sample here (smoothed estimator seeded by this RTT), so
	// an inflated sample shows up close to trueRTT+processHold.
	if got.srtt > trueRTT+processHold/2 {
		t.Errorf("SRTT inflated by internal processing delay (TS path): got %v, want <= %v",
			got.srtt, trueRTT+processHold/2)
	}
}

// TestSRTTNotInflatedByProcessingDelayNoTS covers the non-timestamp main-path
// RTT in sender.handleRcvdSegment.
func TestSRTTNotInflatedByProcessingDelayNoTS(t *testing.T) {
	const (
		trueRTT     = 50 * time.Millisecond
		processHold = 250 * time.Millisecond
	)
	got := runAgedACK(t, false /* useTS */, trueRTT, processHold)
	t.Logf("true RTT=%v hold=%v: SRTT=%v RTO=%v", trueRTT, processHold, got.srtt, got.rto)
	if got.srtt > trueRTT+processHold/2 {
		t.Errorf("SRTT inflated by internal processing delay (non-TS path): got %v, want <= %v",
			got.srtt, trueRTT+processHold/2)
	}
}

// rttTestContext drives real ACK accounting with virtual time. Probe snapshots
// are copied into a bounded channel and read only after the TCP worker drains.
type rttTestContext struct {
	t          *testing.T
	c          *context.Context
	clock      *faketime.ManualClock
	peer       *context.RawEndpoint
	states     chan tcp.TCPSenderState
	timestamps bool
	mss        int
	sent       int
}

func newRTTTestContext(t *testing.T, timestamps bool) *rttTestContext {
	t.Helper()
	h := &rttTestContext{t: t, clock: faketime.NewManualClock(), timestamps: timestamps, states: make(chan tcp.TCPSenderState, 32)}
	h.clock.Advance(time.Second)
	h.c = context.NewWithOpts(t, context.Options{
		EnableV4: true,
		MTU:      mtu,
		Clock:    h.clock,
		Probe: func(state *tcp.TCPEndpointState) {
			select {
			case h.states <- state.Sender:
			default:
			}
		},
	})
	t.Cleanup(h.c.Cleanup)
	e2e.SetStackSACKPermitted(t, h.c, true)
	e2e.SetStackTCPRecovery(t, h.c, int(tcpip.TCPRACKLossDetection))
	if timestamps {
		h.peer = e2e.CreateConnectedWithSACKAndTS(h.c)
	} else {
		h.peer = e2e.CreateConnectedWithSACKPermittedOption(h.c)
	}
	var info tcpip.TCPInfoOption
	if err := h.c.EP.GetSockOpt(&info); err != nil {
		t.Fatalf("GetSockOpt(TCPInfoOption): %s", err)
	}
	h.mss = int(info.SndMss)
	if h.mss <= 1 {
		t.Fatalf("send MSS = %d, need distinct full-sized and runt packets", h.mss)
	}
	return h
}

func (h *rttTestContext) readPacket(offset, size int) uint32 {
	h.t.Helper()
	pkt := h.c.GetPacket()
	defer pkt.Release()
	tcp := header.TCP(header.IPv4(pkt.AsSlice()).Payload())
	if got, want := seqnum.Value(tcp.SequenceNumber()), h.c.IRS.Add(1+seqnum.Size(offset)); got != want {
		h.t.Fatalf("packet sequence = %v, want %v", got, want)
	}
	if got, want := tcp.Payload(), bytes.Repeat([]byte{0xab}, size); !bytes.Equal(got, want) {
		h.t.Fatalf("packet payload = %x, want %x", got, want)
	}
	if got := tcp.ParsedOptions().TS; got != h.timestamps {
		h.t.Fatalf("packet timestamp option = %t, want %t", got, h.timestamps)
	}
	return tcp.ParsedOptions().TSVal
}

func (h *rttTestContext) send(size int) uint32 {
	h.t.Helper()
	data := bytes.Repeat([]byte{0xab}, size)
	if n, err := h.c.EP.Write(bytes.NewReader(data), tcpip.WriteOptions{}); err != nil || n != int64(size) {
		h.t.Fatalf("Write = (%d, %v), want (%d, nil)", n, err, size)
	}
	ts := h.readPacket(h.sent, size)
	h.sent += size
	return ts
}

func (h *rttTestContext) ack(received int, echo uint32, sacks ...header.SACKBlock) tcp.TCPSenderState {
	h.t.Helper()
	ack := h.c.IRS.Add(1 + seqnum.Size(received))
	if h.timestamps {
		if len(sacks) != 0 {
			h.t.Fatal("timestamp ACK helper does not encode SACK blocks")
		}
		h.peer.AckNum = ack
		h.peer.Flags = header.TCPFlagAck
		h.peer.RecentTS = echo
		h.peer.SendPacketWithTS(nil, h.peer.TSVal+1)
	} else {
		h.c.SendAckWithSACK(seqnum.Value(context.TestInitialSequenceNumber).Add(1), received, sacks)
	}
	// Pause drains the queued ACK while virtual time stays fixed. This is the
	// same synchronization used by the existing CUBIC packet fixtures.
	h.c.Stack().Pause()
	h.c.Stack().Resume()
	var last tcp.TCPSenderState
	found := false
	for len(h.states) != 0 {
		last = <-h.states
		found = true
	}
	if !found || last.SndUna != ack {
		h.t.Fatalf("ACK state: found=%t SndUna=%v, want %v", found, last.SndUna, ack)
	}
	return last
}

func (h *rttTestContext) exchange(size int, rtt time.Duration) tcp.TCPSenderState {
	h.t.Helper()
	ts := h.send(size)
	h.clock.Advance(rtt)
	st := h.ack(h.sent, ts)
	if got := st.RACKState.RTT; got != rtt {
		h.t.Fatalf("RACK RTT = %v, want %v", got, rtt)
	}
	return st
}

// TestRACKACKHistory records the current lifetime-minimum behavior through the
// production ACK path. In particular, the sustained histories end with high
// samples; a fresh low sample must not hide a future eligibility/aging bug.
// Six minutes crosses Linux's default minimum-RTT window, but gVisor currently
// has no expiration policy. These are diagnostic expectations, not a proposed
// aging contract or a claim that packet size identifies delayed ACKs.
func TestRACKACKHistory(t *testing.T) {
	for _, test := range []struct {
		name    string
		runt    bool
		samples []int
		idle    time.Duration
	}{
		{name: "sustained_full_sized", samples: []int{100, 100, 100, 100, 100, 100}, idle: time.Minute},
		{name: "sustained_lone_runt_delayed", runt: true, samples: []int{100, 100, 100, 100, 100, 100}, idle: time.Minute},
		{name: "unchanged_path", samples: []int{10, 10, 10, 10, 10, 10}, idle: time.Minute},
		{name: "temporary_spike_then_low", samples: []int{100, 10, 10, 10, 10, 10}, idle: time.Minute},
		{name: "idle_then_high", samples: []int{100}, idle: 6 * time.Minute},
	} {
		t.Run(test.name, func(t *testing.T) {
			h := newRTTTestContext(t, false)
			_ = h.exchange(h.mss, 10*time.Millisecond)
			size := h.mss
			if test.runt {
				size = 1
			}
			var st tcp.TCPSenderState
			for _, rttMillis := range test.samples {
				// The previous flight is fully acknowledged before the idle
				// interval, so advancing the clock cannot retransmit it.
				h.clock.Advance(test.idle)
				st = h.exchange(size, time.Duration(rttMillis)*time.Millisecond)
				if got, want := st.RACKState.ReoWnd, 2500*time.Microsecond; got != want {
					t.Fatalf("reorder window = %v, want current lifetime minimum / 4 = %v", got, want)
				}
			}
			if got, want := h.c.Stack().Stats().TCP.Retransmits.Value(), uint64(0); got != want {
				t.Fatalf("history retransmissions = %d, want %d", got, want)
			}
			t.Logf("payload=%d MSS=%d samples=%d elapsed=%v latest_RTT=%v ReoWnd=%v SRTT=%v", size, h.mss, len(test.samples), h.clock.NowMonotonic().Sub(tcpip.MonotonicTime{}.Add(time.Second)), st.RACKState.RTT, st.RACKState.ReoWnd, st.RTTState.SRTT)

			if test.name != "sustained_full_sized" {
				return
			}
			// Follow the longer path with actual reordered delivery: the
			// newer full-sized segment is SACKed at 101ms, while the original
			// older segment is acknowledged at 110ms. Observe the real timer
			// and retransmitted wire packet before that original ACK.
			start := h.sent
			_ = h.send(h.mss)
			h.clock.Advance(time.Millisecond)
			_ = h.send(h.mss)
			h.clock.Advance(100 * time.Millisecond)
			st = h.ack(start, 0, header.SACKBlock{Start: h.c.IRS.Add(1 + seqnum.Size(start+h.mss)), End: h.c.IRS.Add(1 + seqnum.Size(h.sent))})
			if got, want := st.SackedOut, 1; got != want {
				t.Fatalf("SACKed packets = %d, want %d", got, want)
			}
			h.clock.Advance(8 * time.Millisecond)
			_ = h.readPacket(start, h.mss)
			if got, want := h.c.Stack().Stats().TCP.Retransmits.Value(), uint64(1); got != want {
				t.Fatalf("retransmissions before original ACK = %d, want %d", got, want)
			}
			var info tcpip.TCPInfoOption
			if err := h.c.EP.GetSockOpt(&info); err != nil {
				t.Fatalf("GetSockOpt(TCPInfoOption): %s", err)
			}
			if got, want := info.CcState, tcpip.SACKRecovery; got != want {
				t.Fatalf("congestion state before original ACK = %v, want %v", got, want)
			}
			h.clock.Advance(time.Millisecond)
			st = h.ack(h.sent, 0)
			if got, want := st.Outstanding, 0; got != want {
				t.Fatalf("outstanding after original ACK = %d, want %d", got, want)
			}
			t.Logf("original_ACK=110ms retransmissions_before_original=1 outstanding_after_ACK=%d cwnd_after_recovery=%d", st.Outstanding, st.SndCwnd)
		})
	}
}

// TestRACKRetransmittedACKSample distinguishes RACK's timestamp/minimum checks
// from the sender's SRTT sample selection. An ACK with no timestamp cannot say
// which transmission arrived; the latest RACK sample is still accepted when it
// is at least the current minimum. An echo of the original transmission or a
// sample below that minimum is rejected.
func TestRACKRetransmittedACKSample(t *testing.T) {
	for _, test := range []struct {
		name         string
		timestamps   bool
		echoOriginal bool
		rtt          time.Duration
		wantRTT      time.Duration
	}{
		{name: "no_timestamp_ambiguous", rtt: 50 * time.Millisecond, wantRTT: 50 * time.Millisecond},
		{name: "no_timestamp_below_minimum", rtt: 5 * time.Millisecond, wantRTT: 10 * time.Millisecond},
		{name: "original_timestamp", timestamps: true, echoOriginal: true, rtt: 50 * time.Millisecond, wantRTT: 10 * time.Millisecond},
		{name: "retransmit_timestamp", timestamps: true, rtt: 50 * time.Millisecond, wantRTT: 50 * time.Millisecond},
	} {
		t.Run(test.name, func(t *testing.T) {
			h := newRTTTestContext(t, test.timestamps)
			before := h.exchange(h.mss, 10*time.Millisecond)
			start := h.sent
			originalTS := h.send(h.mss)
			// One outstanding packet uses 2*SRTT + the 200ms delayed-ACK
			// allowance. Advance to that PTO, below the test's 3s RTO.
			pto := 2*before.RTTState.SRTT + 200*time.Millisecond
			if pto >= before.RTO {
				t.Fatalf("PTO %v is not before RTO %v", pto, before.RTO)
			}
			h.clock.Advance(pto)
			retransmitTS := h.readPacket(start, h.mss)
			if got, want := h.c.Stack().Stats().TCP.Retransmits.Value(), uint64(1); got != want {
				t.Fatalf("PTO retransmissions = %d, want %d", got, want)
			}
			echo := retransmitTS
			if test.echoOriginal {
				echo = originalTS
			}
			if test.timestamps && (originalTS == 0 || retransmitTS <= originalTS) {
				t.Fatalf("original/retransmit timestamps = %d/%d, want distinct nonzero timestamps", originalTS, retransmitTS)
			}
			h.clock.Advance(test.rtt)
			after := h.ack(h.sent, echo)
			if got := after.RACKState.RTT; got != test.wantRTT {
				t.Fatalf("RACK RTT after retransmitted ACK = %v, want %v", got, test.wantRTT)
			}
			t.Logf("PTO=%v ACK_after_retransmit=%v original_TS=%d retransmit_TS=%d echoed_TS=%d RACK_before=%v RACK_after=%v SRTT_before=%v SRTT_after=%v", pto, test.rtt, originalTS, retransmitTS, echo, before.RACKState.RTT, after.RACKState.RTT, before.RTTState.SRTT, after.RTTState.SRTT)
		})
	}
}
