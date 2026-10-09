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
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/faketime"
	"gvisor.dev/gvisor/pkg/tcpip/seqnum"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
)

// newTestCubic initializes a sender with a 1000-segment window.
func newTestCubic(t *testing.T, clock *faketime.ManualClock, rtt time.Duration) *cubicState {
	t.Helper()
	s := stack.New(stack.Options{Clock: clock})
	t.Cleanup(func() {
		s.Close()
		s.Wait()
	})
	snd := &sender{
		ep:          &Endpoint{stack: s},
		cwndLimited: true,
		TCPSenderState: TCPSenderState{
			SndCwnd:  1000,
			Ssthresh: InitialSsthresh,
		},
	}
	snd.ep.mu.Lock()
	defer snd.ep.mu.Unlock()
	snd.rtt.Lock()
	snd.rtt.TCPRTTState.SRTT = rtt
	snd.rtt.Unlock()
	c := newCubicCC(snd)
	snd.cc = c
	return c
}

func TestCubicCongestionAvoidanceLimitsGrowth(t *testing.T) {
	clock := faketime.NewManualClock()
	const rtt = 100 * time.Millisecond
	c := newTestCubic(t, clock, rtt)
	c.s.ep.mu.Lock()
	defer c.s.ep.mu.Unlock()
	c.HandleLossDetected()
	c.s.leaveRecovery()
	initial := c.s.SndCwnd
	clock.Advance(time.Minute)
	c.Update(10, rtt, clock.NowMonotonic())
	// Even when the time-based target is far ahead, congestion avoidance
	// must grow slower than slow start (RFC 9438 section 4.2).
	if got := c.s.SndCwnd; got > initial+5 {
		t.Fatalf("acknowledging 10 segments grew the window from %d to %d, want <= %d", initial, got, initial+5)
	}
}

func TestCubicCongestionAvoidanceNeedsAcknowledgments(t *testing.T) {
	clock := faketime.NewManualClock()
	const rtt = time.Millisecond
	c := newTestCubic(t, clock, rtt)
	c.s.ep.mu.Lock()
	defer c.s.ep.mu.Unlock()
	c.HandleLossDetected()
	c.s.leaveRecovery()
	clock.Advance(rtt)
	c.Update(3, rtt, clock.NowMonotonic())
	initial := c.s.SndCwnd
	credit := c.s.SndCAAckCount
	estimate := c.WEst
	if credit == 0 {
		t.Fatal("ACKs did not accumulate growth credit")
	}
	clock.Advance(time.Minute)
	c.Update(0, rtt, clock.NowMonotonic())
	if got := c.s.SndCwnd; got != initial {
		t.Fatalf("without acknowledged segments, cwnd = %d, want %d", got, initial)
	}
	if got := c.s.SndCAAckCount; got != credit {
		t.Fatalf("without acknowledged segments, ACK credit = %d, want %d", got, credit)
	}
	if got := c.WEst; got != estimate {
		t.Fatalf("without acknowledged segments, Reno estimate = %f, want %f", got, estimate)
	}
}

func TestCubicRecoveryDiscardsACKCredit(t *testing.T) {
	clock := faketime.NewManualClock()
	const rtt = time.Millisecond
	c := newTestCubic(t, clock, rtt)
	c.s.ep.mu.Lock()
	defer c.s.ep.mu.Unlock()
	c.HandleLossDetected()
	c.s.leaveRecovery()
	clock.Advance(rtt)
	c.Update(3, rtt, clock.NowMonotonic())
	if c.s.SndCAAckCount == 0 {
		t.Fatal("ACKs did not accumulate cubic growth credit")
	}

	c.HandleLossDetected()
	c.s.leaveRecovery()
	initial := c.s.SndCwnd
	clock.Advance(time.Minute)
	c.Update(1, rtt, clock.NowMonotonic())
	if got := c.s.SndCwnd; got != initial {
		t.Fatalf("first ACK after recovery spent earlier credit: cwnd = %d, want %d", got, initial)
	}
}

func TestCubicIdleExcludesOnlyCurrentEpoch(t *testing.T) {
	clock := faketime.NewManualClock()
	const rtt = 100 * time.Millisecond
	c := newTestCubic(t, clock, rtt)
	c.s.ep.mu.Lock()
	defer c.s.ep.mu.Unlock()
	c.HandleCwndUsage(false)
	clock.Advance(time.Second)
	// Zero-window probing may start a new epoch before application data
	// can resume. Time before that epoch must not shift its origin.
	c.HandleRTOExpired()
	clock.Advance(rtt)
	c.HandleCwndUsage(true)
	if elapsed := clock.NowMonotonic().Sub(c.T); elapsed != 0 {
		t.Fatalf("epoch age after idle restart = %s, want 0", elapsed)
	}
}

func TestCubicFlightUtilization(t *testing.T) {
	for _, test := range []struct {
		name  string
		start seqnum.Value
	}{
		{"normal", 1},
		{"wrap", ^seqnum.Value(0) - 5},
	} {
		t.Run(test.name, func(t *testing.T) {
			clock := faketime.NewManualClock()
			const rtt = 100 * time.Millisecond
			c := newTestCubic(t, clock, rtt)
			c.s.ep.mu.Lock()
			defer c.s.ep.mu.Unlock()
			c.s.SndCwnd = 10
			c.s.Ssthresh = 10
			c.enterCongestionAvoidance()
			c.s.cwndLimited = false
			c.s.SndUna = test.start
			c.s.SndNxt = test.start.Add(2)
			c.s.Outstanding = 2
			c.s.updateCwndUsage()
			// A later send can fill a previously partial flight, including
			// sends from SACK/RACK recovery through postXmit.
			c.s.SndNxt = test.start.Add(10)
			c.s.Outstanding = 10
			c.s.updateCwndUsage()

			// Removing packets on ACK must not erase evidence that the
			// window was full, including at sequence-number wrap.
			c.s.SndUna = c.s.SndNxt - 1
			c.s.Outstanding = 1
			c.Update(9, rtt, clock.NowMonotonic())
			c.s.updateCwndUsage()
			c.s.SndUna = c.s.SndNxt
			c.s.Outstanding = 0
			c.Update(1, rtt, clock.NowMonotonic())
			if got := c.s.SndCwnd; got != 11 {
				t.Fatalf("last ACK of full flight: cwnd = %d, want 11", got)
			}
			c.s.updateCwndUsage()
			if c.s.isCwndLimited() || !c.paused {
				t.Fatal("retired full flight still permits growth")
			}

			// Repeated no-send calls, including a closed receive window,
			// must neither retain the old flight nor restart the pause.
			c.s.SndWnd = 0
			clock.Advance(time.Second)
			c.s.updateCwndUsage()
			clock.Advance(time.Second)
			c.s.SndWnd = 30000
			c.s.Outstanding = 2
			c.s.SndNxt.UpdateForward(2)
			c.s.updateCwndUsage()
			clock.Advance(time.Second)
			c.s.Outstanding = c.s.SndCwnd
			c.s.SndNxt.UpdateForward(seqnum.Size(c.s.SndCwnd - 2))
			c.s.updateCwndUsage()
			clock.Advance(rtt)
			if age := clock.NowMonotonic().Sub(c.T); age != rtt {
				t.Fatalf("epoch age after resumed full flight = %s, want %s", age, rtt)
			}
		})
	}
}

func TestCubicHyStartInitializesCongestionAvoidance(t *testing.T) {
	clock := faketime.NewManualClock()
	const rtt = 100 * time.Millisecond
	c := newTestCubic(t, clock, rtt)
	c.s.ep.mu.Lock()
	defer c.s.ep.mu.Unlock()
	c.LastRTT = rtt
	c.CurrRTT = 2 * rtt
	c.SampleCount = nRTTSample - 1
	c.s.cwndLimited = false
	clock.Advance(rtt)
	c.Update(1, 2*rtt, clock.NowMonotonic())
	if c.s.Ssthresh != c.s.SndCwnd {
		t.Fatal("HyStart did not end slow start")
	}
	if got, want := c.WMax, float64(c.s.SndCwnd); got != want {
		t.Errorf("initial congestion-avoidance maximum = %f, want %f", got, want)
	}
	if age := clock.NowMonotonic().Sub(c.T); age != 0 {
		t.Errorf("initial congestion-avoidance epoch age = %s, want 0", age)
	}
}

func TestCubicSlowStartUsesFlightSize(t *testing.T) {
	for _, test := range []struct {
		name        string
		outstanding int
		wantGrowth  bool
	}{
		{"half-window", 5, false},
		{"above-half-window", 6, true},
	} {
		t.Run(test.name, func(t *testing.T) {
			clock := faketime.NewManualClock()
			const rtt = 100 * time.Millisecond
			c := newTestCubic(t, clock, rtt)
			c.s.ep.mu.Lock()
			defer c.s.ep.mu.Unlock()
			c.s.SndCwnd = 10
			c.s.cwndLimited = false
			c.s.SndNxt = seqnum.Value(test.outstanding)
			c.s.Outstanding = test.outstanding
			c.s.updateCwndUsage()
			c.s.SndUna = c.s.SndNxt
			c.s.Outstanding = 0
			c.Update(test.outstanding, rtt, clock.NowMonotonic())
			if grew := c.s.SndCwnd > 10; grew != test.wantGrowth {
				t.Errorf("ACKed %d packets: cwnd = %d, want growth=%t", test.outstanding, c.s.SndCwnd, test.wantGrowth)
			}
		})
	}
}

func TestCubicFriendlyEstimateUsesACKs(t *testing.T) {
	estimate := func(elapsed time.Duration, packetsAcked int) float64 {
		clock := faketime.NewManualClock()
		const rtt = 100 * time.Millisecond
		c := newTestCubic(t, clock, rtt)
		c.s.ep.mu.Lock()
		defer c.s.ep.mu.Unlock()
		c.HandleLossDetected()
		c.s.leaveRecovery()
		clock.Advance(elapsed)
		c.Update(packetsAcked, rtt, clock.NowMonotonic())
		return c.WEst
	}
	first := estimate(0, 10)
	if delayed := estimate(time.Second, 10); delayed != first {
		t.Errorf("equal ACK counts give different Reno estimates: immediate=%f delayed=%f", first, delayed)
	}
	if more := estimate(0, 20); more <= first {
		t.Errorf("more ACKs did not increase the Reno estimate: first=%f more=%f", first, more)
	}
}

func TestCubicFriendlyEstimateAbovePriorWindow(t *testing.T) {
	clock := faketime.NewManualClock()
	const rtt = 100 * time.Millisecond
	c := newTestCubic(t, clock, rtt)
	c.s.ep.mu.Lock()
	defer c.s.ep.mu.Unlock()
	c.s.SndCwnd = 10
	c.HandleLossDetected()
	c.s.leaveRecovery()
	for range 6 {
		c.Update(c.s.SndCwnd, rtt, clock.NowMonotonic())
	}
	if c.WEst < c.WLastMax {
		t.Fatal("estimate did not regain the pre-loss window")
	}
	previous := c.WEst
	c.Update(c.s.SndCwnd, rtt, clock.NowMonotonic())
	if increase := c.WEst - previous; increase != 1 {
		t.Fatalf("estimate increase above the prior window = %f, want 1", increase)
	}
}

func TestCubicFriendlyRegionConsumesOldCredit(t *testing.T) {
	clock := faketime.NewManualClock()
	const rtt = time.Millisecond
	c := newTestCubic(t, clock, rtt)
	c.s.ep.mu.Lock()
	defer c.s.ep.mu.Unlock()
	c.HandleLossDetected()
	c.s.leaveRecovery()
	clock.Advance(rtt)
	c.Update(3, rtt, clock.NowMonotonic())
	if c.s.SndCAAckCount == 0 {
		t.Fatal("ACKs did not accumulate cubic growth credit")
	}
	c.Update(c.s.SndCwnd, rtt, clock.NowMonotonic())
	if c.WC >= c.WEst {
		t.Fatal("ACKs did not reach the Reno-friendly region")
	}
	initial := c.s.SndCwnd
	clock.Advance(time.Minute)
	c.Update(1, rtt, clock.NowMonotonic())
	if c.s.SndCwnd != initial {
		t.Fatalf("returning to the cubic region reused ACK credit: window=%d, want %d", c.s.SndCwnd, initial)
	}
}

func TestCubicFastConvergenceStartsAtCurrentWindow(t *testing.T) {
	clock := faketime.NewManualClock()
	c := newTestCubic(t, clock, 100*time.Millisecond)
	c.s.ep.mu.Lock()
	defer c.s.ep.mu.Unlock()
	for range 2 {
		c.HandleLossDetected()
		c.s.leaveRecovery()
	}
	// Fast convergence changes the remembered maximum. The next epoch
	// must still begin at the window the sender actually retained.
	origin := c.cubicCwnd(-c.K)
	want := float64(c.s.SndCwnd)
	if origin < want-1 || origin > want+1 {
		t.Fatalf("epoch curve starts at %f segments, want within one segment of %f", origin, want)
	}
}

func TestCubicSlowStartPreservesExcessACKs(t *testing.T) {
	stateAfter := func(acks ...int) (int, float64) {
		clock := faketime.NewManualClock()
		const rtt = 100 * time.Millisecond
		c := newTestCubic(t, clock, rtt)
		c.s.ep.mu.Lock()
		defer c.s.ep.mu.Unlock()
		c.s.Ssthresh = c.s.SndCwnd + 5
		for _, n := range acks {
			c.Update(n, rtt, clock.NowMonotonic())
		}
		return c.s.SndCwnd, c.WEst
	}
	// Crossing ssthresh in one ACK must preserve the same credit as
	// acknowledging the slow-start and congestion-avoidance data separately.
	gotWindow, gotEstimate := stateAfter(8)
	wantWindow, wantEstimate := stateAfter(5, 3)
	if wantEstimate <= float64(wantWindow) {
		t.Fatal("separate ACKs did not retain fractional growth credit")
	}
	if gotWindow != wantWindow || gotEstimate != wantEstimate {
		t.Fatalf("crossing ssthresh gives (%d, %f), want (%d, %f)", gotWindow, gotEstimate, wantWindow, wantEstimate)
	}
}

func TestCubicRTOPreservesPriorWindow(t *testing.T) {
	clock := faketime.NewManualClock()
	const rtt = 100 * time.Millisecond
	c := newTestCubic(t, clock, rtt)
	c.s.ep.mu.Lock()
	defer c.s.ep.mu.Unlock()
	c.HandleLossDetected()
	c.s.leaveRecovery()
	prior := float64(c.s.SndCwnd)
	c.HandleRTOExpired()
	c.Update(c.s.Ssthresh-c.s.SndCwnd, rtt, clock.NowMonotonic())
	if c.WLastMax != prior {
		t.Fatalf("prior window after RTO slow start = %f, want %f", c.WLastMax, prior)
	}
	if got, want := c.WEst, float64(c.s.SndCwnd); got != want {
		t.Fatalf("Reno estimate at RTO slow-start exit = %f, want %f", got, want)
	}
}

func TestCubicWindowRestartPreservesPeak(t *testing.T) {
	for _, recovery := range []string{"fast", "timeout"} {
		t.Run(recovery, func(t *testing.T) {
			clock := faketime.NewManualClock()
			const rtt = 100 * time.Millisecond
			c := newTestCubic(t, clock, rtt)
			c.s.ep.mu.Lock()
			defer c.s.ep.mu.Unlock()
			c.s.SndCwnd = 7
			if recovery == "timeout" {
				c.HandleRTOExpired()
				c.Update(c.s.Ssthresh-c.s.SndCwnd, rtt, clock.NowMonotonic())
			} else {
				c.HandleLossDetected()
				c.s.leaveRecovery()
			}
			peak := c.WMax
			clock.Advance(time.Minute)
			for range 3 {
				c.Update(c.s.SndCwnd, rtt, clock.NowMonotonic())
			}
			if c.s.SndCwnd <= InitialCwnd {
				t.Fatal("window did not grow beyond the restart window")
			}
			c.s.SndCwnd = InitialCwnd
			c.HandleWindowRestart()
			if c.WMax != peak {
				t.Errorf("restart changed the remembered peak from %f to %f", peak, c.WMax)
			}
			if origin := c.cubicCwnd(-c.K); origin != InitialCwnd {
				t.Errorf("restart curve origin = %f, want %d", origin, InitialCwnd)
			}
			if c.WEst != InitialCwnd || c.s.SndCAAckCount != 0 {
				t.Errorf("restart retained old growth credit: estimate=%f ACKs=%d", c.WEst, c.s.SndCAAckCount)
			}
		})
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
