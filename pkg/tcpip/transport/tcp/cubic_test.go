// Copyright 2024 The gVisor Authors.
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

package tcp

import (
	"fmt"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/faketime"
	"gvisor.dev/gvisor/pkg/tcpip/seqnum"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
)

// cubicAfterRecovery starts congestion avoidance after losing a packet from
// a 1000-segment window.
func cubicAfterRecovery(t *testing.T, clock *faketime.ManualClock, rtt time.Duration) *cubicState {
	t.Helper()
	s := stack.New(stack.Options{Clock: clock})
	t.Cleanup(func() {
		s.Close()
		s.Wait()
	})
	ep := &Endpoint{stack: s}
	ep.mu.Lock()
	defer ep.mu.Unlock()
	snd := &sender{
		ep: ep,
		TCPSenderState: TCPSenderState{
			SndCwnd:  1000,
			Ssthresh: InitialSsthresh,
		},
	}
	snd.rtt.Lock()
	snd.rtt.TCPRTTState.SRTT = rtt
	snd.rtt.Unlock()
	c := newCubicCC(snd)
	c.HandleLossDetected()
	snd.SndCwnd = snd.Ssthresh
	c.PostRecovery()
	return c
}

func TestCubicCongestionAvoidanceAccumulatesACKs(t *testing.T) {
	for _, acked := range []int{1, 2, 10} {
		t.Run(fmt.Sprintf("segments_per_ack=%d", acked), func(t *testing.T) {
			clock := faketime.NewManualClock()
			const rtt = 100 * time.Millisecond
			c := cubicAfterRecovery(t, clock, rtt)
			c.s.ep.mu.Lock()
			defer c.s.ep.mu.Unlock()
			initial := c.s.SndCwnd
			// Deliver one window's ACKs over one RTT. At this window and
			// RTT, CUBIC must recover some capacity after the loss, even
			// when each individual increase is less than a segment.
			for acknowledged := 0; acknowledged < initial; acknowledged += acked {
				clock.Advance(rtt * time.Duration(acked) / time.Duration(initial))
				c.Update(acked, rtt, clock.NowMonotonic())
			}
			if got := c.s.SndCwnd; got <= initial {
				t.Fatalf("window after acknowledging %d segments = %d, want > %d", initial, got, initial)
			}
		})
	}
}

func TestCubicCongestionAvoidanceLimitsGrowth(t *testing.T) {
	clock := faketime.NewManualClock()
	const rtt = 100 * time.Millisecond
	c := cubicAfterRecovery(t, clock, rtt)
	c.s.ep.mu.Lock()
	defer c.s.ep.mu.Unlock()
	initial := c.s.SndCwnd
	clock.Advance(time.Minute)
	c.Update(1, rtt, clock.NowMonotonic())
	// Even when the time-based target is far ahead, congestion avoidance
	// must grow slower than slow start (RFC 9438 section 4.2).
	if got := c.s.SndCwnd; got > initial+1 {
		t.Fatalf("one ACK grew the window from %d to %d, want <= %d", initial, got, initial+1)
	}
}

// TestHyStartAckTrainOK tests that HyStart triggers early exit from slow start
// if ACKs come in the same round for longer than RTT/2.
func TestHyStartAckTrainOK(t *testing.T) {
	fClock := faketime.NewManualClock()
	stackOpts := stack.Options{
		TransportProtocols: []stack.TransportProtocolFactory{NewProtocol},
		Clock:              fClock,
	}
	s := stack.New(stackOpts)
	ep := &Endpoint{
		stack: s,
		cc:    tcpip.CongestionControlOption("cubic"),
	}
	iss := seqnum.Value(0)
	snd := &sender{
		ep: ep,
		TCPSenderState: TCPSenderState{
			SndUna:   iss + 1,
			SndNxt:   iss + 1,
			Ssthresh: InitialSsthresh,
		},
	}
	snd.ep.mu.Lock()
	uut := newCubicCC(snd)
	snd.ep.mu.Unlock()
	snd.cc = uut

	if uut.LastRTT != effectivelyInfinity {
		t.Fatal()
	}
	if uut.CurrRTT != effectivelyInfinity {
		t.Fatal()
	}

	d0 := 4 * time.Millisecond
	uut.s.ep.mu.Lock()
	defer uut.s.ep.mu.Unlock()
	uut.updateHyStart(d0, fClock.NowMonotonic())
	if uut.CurrRTT != d0 {
		t.Fatal()
	}
	if snd.Ssthresh != InitialSsthresh {
		t.Fatal("HyStart should not be triggered")
	}

	// Move SndNext and SndUna to advance to a new round.
	snd.SndNxt = snd.SndNxt.Add(2000)
	snd.SndUna = snd.SndUna.Add(1000)
	fClock.Advance(d0)
	r1ExpectedStart := fClock.NowMonotonic()

	d1 := 5 * time.Millisecond
	uut.updateHyStart(d1, fClock.NowMonotonic())
	if uut.LastRTT != d0 {
		t.Fatal()
	}
	if uut.CurrRTT != d1 {
		t.Fatal()
	}
	if uut.RoundStart != r1ExpectedStart {
		t.Fatal()
	}

	// Still in round after RTT/2 (2ms) triggers HyStart.  Note that HyStart
	// will ignore ACKs spaced more than 2ms apart, so we send one per ms 3
	// times.
	for range 2 {
		fClock.Advance(time.Millisecond)
		uut.updateHyStart(d1, fClock.NowMonotonic())
		if snd.Ssthresh != InitialSsthresh {
			t.Fatal("HyStart should not be triggered")
		}
		if uut.LastAck != fClock.NowMonotonic() {
			t.Fatal()
		}
	}

	// 3 ms---triggers HyStart setting Ssthresh
	fClock.Advance(time.Millisecond)
	uut.updateHyStart(d1, fClock.NowMonotonic())
	if snd.Ssthresh == InitialSsthresh {
		t.Fatal("HyStart SHOULD be triggered")
	}
}

// TestHyStartAckTrainTooSpread tests that ACKs that are more than 2ms apart
// are ignored for purposes of triggering HyStart via the ACK train mechanism.
func TestHyStartAckTrainTooSpread(t *testing.T) {
	fClock := faketime.NewManualClock()
	stackOpts := stack.Options{
		TransportProtocols: []stack.TransportProtocolFactory{NewProtocol},
		Clock:              fClock,
	}
	s := stack.New(stackOpts)
	ep := &Endpoint{
		stack: s,
		cc:    tcpip.CongestionControlOption("cubic"),
	}
	iss := seqnum.Value(0)
	snd := &sender{
		ep: ep,
		TCPSenderState: TCPSenderState{
			SndUna:   iss + 1,
			SndNxt:   iss + 1,
			Ssthresh: InitialSsthresh,
		},
	}
	snd.ep.mu.Lock()
	uut := newCubicCC(snd)
	snd.ep.mu.Unlock()
	snd.cc = uut

	if uut.LastRTT != effectivelyInfinity {
		t.Fatal()
	}
	if uut.CurrRTT != effectivelyInfinity {
		t.Fatal()
	}
	d0 := 4 * time.Millisecond
	uut.s.ep.mu.Lock()
	defer uut.s.ep.mu.Unlock()
	uut.updateHyStart(d0, fClock.NowMonotonic())
	if uut.CurrRTT != d0 {
		t.Fatal()
	}
	if snd.Ssthresh != InitialSsthresh {
		t.Fatal("HyStart should not be triggered")
	}

	// Move SndNext and SndUna to advance to a new round.
	snd.SndNxt = snd.SndNxt.Add(2000)
	snd.SndUna = snd.SndUna.Add(1000)
	fClock.Advance(d0)
	r1ExpectedStart := fClock.NowMonotonic()

	d1 := 5 * time.Millisecond
	uut.updateHyStart(d1, fClock.NowMonotonic())
	if uut.LastRTT != d0 {
		t.Fatal()
	}
	if uut.CurrRTT != d1 {
		t.Fatal()
	}
	if uut.RoundStart != r1ExpectedStart {
		t.Fatal()
	}

	// HyStart will ignore ACKs spaced more than 2ms apart
	fClock.Advance(3 * time.Millisecond)
	uut.updateHyStart(d1, fClock.NowMonotonic())
	if snd.Ssthresh != InitialSsthresh {
		t.Fatal("HyStart should not be triggered")
	}
	if uut.LastAck != r1ExpectedStart {
		t.Fatal("Should ignore ACK 3ms later")
	}
}

// TestHyStartDelayOK tests that HyStart triggers early exit from slow start
// if RTT exceeds previous round by at least minRTTThresh.
func TestHyStartDelayOK(t *testing.T) {
	fClock := faketime.NewManualClock()
	stackOpts := stack.Options{
		TransportProtocols: []stack.TransportProtocolFactory{NewProtocol},
		Clock:              fClock,
	}
	s := stack.New(stackOpts)
	ep := &Endpoint{
		stack: s,
		cc:    tcpip.CongestionControlOption("cubic"),
	}
	iss := seqnum.Value(0)
	snd := &sender{
		ep: ep,
		TCPSenderState: TCPSenderState{
			SndUna:   iss + 1,
			SndNxt:   iss + 1,
			Ssthresh: InitialSsthresh,
		},
	}
	snd.ep.mu.Lock()
	uut := newCubicCC(snd)
	snd.ep.mu.Unlock()
	snd.cc = uut

	d0 := 4 * time.Millisecond
	uut.s.ep.mu.Lock()
	defer uut.s.ep.mu.Unlock()
	uut.updateHyStart(d0, fClock.NowMonotonic())

	// Move SndNext and SndUna to advance to a new round.
	snd.SndNxt = snd.SndNxt.Add(2000)
	snd.SndUna = snd.SndUna.Add(1000)
	fClock.Advance(d0)

	d1 := d0 + minRTTThresh

	// Delay detection requires at least nRTTSample measurements.
	for i := uint(1); i < nRTTSample; i++ {
		uut.updateHyStart(d1, fClock.NowMonotonic())
		if uut.SampleCount != i {
			t.Fatal()
		}
	}
	if snd.Ssthresh != InitialSsthresh {
		t.Fatal("triggered with fewer than nRTTSample measurements")
	}
	uut.updateHyStart(d1, fClock.NowMonotonic())
	if snd.Ssthresh == InitialSsthresh {
		t.Fatal("didn't trigger SS exit")
	}
}

// TestHyStartDelay_BelowThresh tests that HyStart doesn't trigger early exit
// from slow start if at least one RTT measurement is below threshold.
func TestHyStartDelay_BelowThresh(t *testing.T) {
	fClock := faketime.NewManualClock()
	stackOpts := stack.Options{
		TransportProtocols: []stack.TransportProtocolFactory{NewProtocol},
		Clock:              fClock,
	}
	s := stack.New(stackOpts)
	ep := &Endpoint{
		stack: s,
		cc:    tcpip.CongestionControlOption("cubic"),
	}
	iss := seqnum.Value(0)
	snd := &sender{
		ep: ep,
		TCPSenderState: TCPSenderState{
			SndUna:   iss + 1,
			SndNxt:   iss + 1,
			Ssthresh: InitialSsthresh,
		},
	}
	snd.ep.mu.Lock()
	uut := newCubicCC(snd)
	snd.ep.mu.Unlock()
	snd.cc = uut

	d0 := 4 * time.Millisecond
	uut.s.ep.mu.Lock()
	defer uut.s.ep.mu.Unlock()
	uut.updateHyStart(d0, fClock.NowMonotonic())

	// Move SndNext and SndUna to advance to a new round.
	snd.SndNxt = snd.SndNxt.Add(2000)
	snd.SndUna = snd.SndUna.Add(1000)
	fClock.Advance(d0)

	d1 := d0 + minRTTThresh

	// Delay detection requires at least nRTTSample measurements.
	for i := uint(1); i < nRTTSample; i++ {
		uut.updateHyStart(d1, fClock.NowMonotonic())
		if uut.SampleCount != i {
			t.Fatal()
		}
	}
	if snd.Ssthresh != InitialSsthresh {
		t.Fatal("triggered with fewer than nRTTSample measurements")
	}
	uut.updateHyStart(d1-time.Millisecond, fClock.NowMonotonic())
	if snd.Ssthresh != InitialSsthresh {
		t.Fatal("triggered with a measurement under threshold")
	}
}

// TestHyStartAckTrainUsesIngressTime verifies that HyStart's ACK-train detector
// uses each ACK's ingress time (the ackTime argument) rather than the time the
// ACK was processed. This is a regression test for the gvisor#9707/#9778 family:
// if ACKs are delayed inside the stack and processed in a burst (so their
// processing-clock timestamps cluster within ackDelta), measuring against
// processing time would make widely-spaced ACKs look like a tight "train" and
// could trip a premature exit from slow start, capping cwnd on a high-BDP path.
//
// Here the processing clock (fClock) is held essentially still across the ACKs
// (simulating a burst drained after a lock release) while the ackTime arguments
// reflect the ACKs' true arrival, spaced > ackDelta (2ms) apart. With the fix,
// the ACK-train detector ignores them (they are not a train) and HyStart does
// not fire. With the bug (processing-time), they would look like a train and
// fire.
func TestHyStartAckTrainUsesIngressTime(t *testing.T) {
	fClock := faketime.NewManualClock()
	stackOpts := stack.Options{
		TransportProtocols: []stack.TransportProtocolFactory{NewProtocol},
		Clock:              fClock,
	}
	s := stack.New(stackOpts)
	ep := &Endpoint{
		stack: s,
		cc:    tcpip.CongestionControlOption("cubic"),
	}
	iss := seqnum.Value(0)
	snd := &sender{
		ep: ep,
		TCPSenderState: TCPSenderState{
			SndUna:   iss + 1,
			SndNxt:   iss + 1,
			Ssthresh: InitialSsthresh,
		},
	}
	snd.ep.mu.Lock()
	uut := newCubicCC(snd)
	snd.ep.mu.Unlock()
	snd.cc = uut

	uut.s.ep.mu.Lock()
	defer uut.s.ep.mu.Unlock()

	// This mirrors TestHyStartAckTrainOK, which establishes a round and then
	// shows that ACKs spaced 1ms apart (< ackDelta) trigger HyStart once the
	// round has lasted > LastRTT/2. The only difference here: the PROCESSING
	// clock is frozen during the burst (modeling ACKs drained together after a
	// stack-internal delay), and the ACKs' true arrival times — supplied via
	// ackTime — are spaced 3ms apart (> ackDelta), i.e. NOT a real train.
	//
	// With the fix (ackTime is used) the detector sees 3ms spacing and never
	// fires. Without the fix (processing clock is used) the frozen clock makes
	// every burst ACK look simultaneous (spacing 0 < ackDelta) while the round
	// has aged past LastRTT/2, so HyStart fires spuriously.
	d0 := 4 * time.Millisecond
	uut.updateHyStart(d0, fClock.NowMonotonic())

	// Begin the round under test (LastRTT = d0 = 4ms; RoundStart = 4ms).
	snd.SndNxt = snd.SndNxt.Add(2000)
	snd.SndUna = snd.SndUna.Add(1000)
	fClock.Advance(d0)
	roundStart := fClock.NowMonotonic() // t = 4ms
	uut.updateHyStart(5*time.Millisecond, roundStart)

	// Keep the train "warm": process two more ACKs ~1ms apart so LastAck tracks
	// recent processing time. This is the normal in-round state right before a
	// stack-internal delay/burst occurs.
	fClock.Advance(time.Millisecond) // t = 5ms
	uut.updateHyStart(5*time.Millisecond, fClock.NowMonotonic())
	fClock.Advance(time.Millisecond) // t = 6ms; LastAck now 6ms
	uut.updateHyStart(5*time.Millisecond, fClock.NowMonotonic())

	// Now the burst: the processing clock jumps forward (the ACKs were delayed
	// inside the stack) and is then frozen while several ACKs are drained
	// together. Their TRUE arrival times, supplied via ackTime, are spaced 3ms
	// apart (> ackDelta), so they are NOT a real train.
	//
	//   - Fix (ackTime): inter-ACK spacing seen as 3ms >= ackDelta, and after
	//     the first ACK LastAck advances to the (spread) arrival time, so the
	//     train branch is never taken -> HyStart does not fire.
	//   - Bug (processing clock): the frozen processing time is within ackDelta
	//     of the previous LastAck, and after the first burst ACK LastAck = that
	//     frozen time, so every subsequent ACK has spacing 0 < ackDelta while
	//     now - RoundStart (>> 2ms) exceeds LastRTT/2 -> HyStart fires.
	fClock.Advance(time.Millisecond) // t = 7ms: within ackDelta of LastAck(6ms) and
	// 3ms past RoundStart(4ms) > LastRTT/2(2ms); frozen below for the burst.
	arrival := fClock.NowMonotonic()
	for i := 0; i < 4; i++ {
		arrival = arrival.Add(3 * time.Millisecond) // true arrivals 3ms apart
		uut.updateHyStart(5*time.Millisecond, arrival)
		if snd.Ssthresh != InitialSsthresh {
			t.Fatalf("HyStart ACK-train fired on ACKs whose true arrivals are 3ms apart "+
				"(> ackDelta); it measured clustered processing time instead of ingress "+
				"time (iteration %d)", i)
		}
	}
}
