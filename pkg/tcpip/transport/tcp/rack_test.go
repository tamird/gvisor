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

package tcp

import (
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/buffer"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/faketime"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/seqnum"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
)

const testClockResolution = 500 * time.Microsecond

type quantizedClock struct {
	*faketime.ManualClock
	resolution time.Duration
}

func (c *quantizedClock) NowMonotonic() tcpip.MonotonicTime {
	now := c.ManualClock.NowMonotonic().Sub(tcpip.MonotonicTime{})
	return tcpip.MonotonicTime{}.Add(now.Truncate(c.resolution))
}

type rackTestContext struct {
	clock tcpip.Clock
	snd   sender
	segs  []*segment
}

func newRACKTestContext(clock tcpip.Clock, resolution time.Duration) *rackTestContext {
	ctx := &rackTestContext{clock: clock}
	st := stack.New(stack.Options{Clock: clock, ClockResolution: resolution})
	ctx.snd.ep = &Endpoint{
		stack:      st,
		scoreboard: NewSACKScoreboard(1, 0),
	}
	ctx.snd.writeList.set = make(map[*segment]struct{})
	ctx.snd.cc = newRenoCC(&ctx.snd)
	ctx.snd.reorderTimer.init(clock, func() {})
	ctx.snd.rc.init(&ctx.snd, 0)
	return ctx
}

// +checklocks:ctx.snd.ep.mu
func (ctx *rackTestContext) addSegment(seq seqnum.Value, xmitTime tcpip.MonotonicTime) *segment {
	seg := newOutgoingSegment(
		stack.TransportEndpointID{},
		ctx.clock,
		buffer.MakeWithView(buffer.NewViewSize(1)),
		0,
	)
	seg.sequenceNumber = seq
	seg.xmitCount = 1
	seg.xmitTime = xmitTime
	ctx.snd.writeList.PushBack(seg)
	ctx.segs = append(ctx.segs, seg)
	return seg
}

func (ctx *rackTestContext) cleanup() {
	ctx.snd.reorderTimer.cleanup()
	for _, seg := range ctx.segs {
		seg.DecRef()
	}
}

func TestRACKQuantizedClockPreventsPrematureLossAndSchedulesRecheck(t *testing.T) {
	clock := &quantizedClock{
		ManualClock: faketime.NewManualClock(),
		resolution:  testClockResolution,
	}
	ctx := newRACKTestContext(clock, testClockResolution)
	defer ctx.cleanup()
	ctx.snd.ep.mu.Lock()
	defer ctx.snd.ep.mu.Unlock()

	// Send the older packet just before a quantization boundary.
	clock.Advance(testClockResolution - time.Microsecond)
	older := ctx.addSegment(0, clock.NowMonotonic())

	// Send the newer packet just after the boundary, then receive its ACK
	// within the same clock bucket.
	clock.Advance(2 * time.Microsecond)
	newer := ctx.addSegment(1, clock.NowMonotonic())
	clock.Advance(49 * time.Microsecond)
	ack := newOutgoingSegment(stack.TransportEndpointID{}, clock, buffer.Buffer{}, 0)
	defer ack.DecRef()

	ctx.snd.rc.update(newer, ack)
	if got := ctx.snd.rc.RTT; got != 0 {
		t.Fatalf("RACK RTT = %v, want 0", got)
	}
	wantXmitTime := (tcpip.MonotonicTime{}).Add(testClockResolution)
	if got := ctx.snd.rc.XmitTime; got != wantXmitTime {
		t.Fatalf("RACK transmit time = %v, want %v", got, wantXmitTime)
	}
	if got := ctx.snd.rc.EndSequence; got != 2 {
		t.Fatalf("RACK end sequence = %d, want 2", got)
	}

	var timerFired bool
	ctx.snd.reorderTimer.cleanup()
	ctx.snd.reorderTimer.init(clock, func() { timerFired = true })
	if got := ctx.snd.rc.detectLoss(ack.rcvdTime); got != 0 {
		t.Fatalf("detectLoss at ACK got %d losses, want 0", got)
	}
	if older.lost {
		t.Fatal("older packet marked lost from quantization alone")
	}
	wantTimerTarget := (tcpip.MonotonicTime{}).Add(2 * testClockResolution)
	if got := ctx.snd.reorderTimer.target; got != wantTimerTarget {
		t.Fatalf("reorder timer target = %v, want %v", got, wantTimerTarget)
	}

	clock.Advance(testClockResolution - time.Nanosecond)
	if timerFired {
		t.Fatal("reorder timer fired before its target")
	}
	clock.Advance(time.Nanosecond)
	if !timerFired {
		t.Fatal("reorder timer did not fire at its target")
	}
	if older.lost {
		t.Fatal("timer callback marked packet lost before endpoint processing")
	}

	// Delay endpoint processing by another tick. The production expiry path
	// evaluates loss at the timer target, where the packet is now beyond the
	// uncertainty boundary, rather than adding callback delay to its age.
	clock.Advance(testClockResolution)
	ctx.snd.FastRecovery.Active = true
	ctx.snd.SndNxt = 1
	ctx.snd.SndCwnd = 0
	if err := ctx.snd.rc.reorderTimerExpired(); err != nil {
		t.Fatalf("reorderTimerExpired failed: %v", err)
	}
	if !older.lost {
		t.Fatal("reorderTimerExpired did not mark older packet lost at timer target")
	}
}

func TestRACKClockResolutionPreventsPrematureLoss(t *testing.T) {
	for _, test := range []struct {
		name       string
		resolution time.Duration
		wantLost   bool
	}{
		{name: "unspecified resolution", wantLost: true},
		{name: "quantization allowance", resolution: testClockResolution},
	} {
		t.Run(test.name, func(t *testing.T) {
			clock := faketime.NewManualClock()
			ctx := newRACKTestContext(clock, test.resolution)
			defer ctx.cleanup()
			ctx.snd.ep.mu.Lock()
			defer ctx.snd.ep.mu.Unlock()

			ctx.snd.rc.XmitTime = tcpip.MonotonicTime{}.Add(testClockResolution)
			ctx.snd.rc.EndSequence = 2
			seg := ctx.addSegment(0, tcpip.MonotonicTime{})

			gotLost := ctx.snd.rc.detectLoss(tcpip.MonotonicTime{}.Add(testClockResolution)) != 0
			if gotLost != test.wantLost || seg.lost != test.wantLost {
				t.Errorf("detectLoss=%t, segment lost=%t; want %t", gotLost, seg.lost, test.wantLost)
			}
		})
	}
}

func TestRACKClockResolutionLossBoundaryIsExclusive(t *testing.T) {
	clock := faketime.NewManualClock()
	ctx := newRACKTestContext(clock, testClockResolution)
	defer ctx.cleanup()
	ctx.snd.ep.mu.Lock()
	defer ctx.snd.ep.mu.Unlock()
	ctx.snd.rc.XmitTime = tcpip.MonotonicTime{}.Add(time.Nanosecond)
	ctx.snd.rc.EndSequence = 2
	seg := ctx.addSegment(0, tcpip.MonotonicTime{})

	if got := ctx.snd.rc.detectLoss(tcpip.MonotonicTime{}.Add(testClockResolution)); got != 0 {
		t.Fatalf("detectLoss at uncertainty boundary got %d losses, want 0", got)
	}
	if seg.lost {
		t.Fatal("segment marked lost at uncertainty boundary")
	}
	if !ctx.snd.reorderTimer.enabled() {
		t.Fatal("reorder timer not enabled at uncertainty boundary")
	}
	if got := ctx.snd.rc.detectLoss(tcpip.MonotonicTime{}.Add(2 * testClockResolution)); got != 1 {
		t.Fatalf("detectLoss after uncertainty boundary got %d losses, want 1", got)
	}
}

// TestRACKReorderTimerDeclaresLossOnFirstExpiry verifies that a reorder timer
// armed on a quantized clock expires exactly once, at the loss deadline
// including timestamp uncertainty, and that its first expiry declares loss.
// Arming only to the pre-uncertainty deadline makes every expiry a no-op that
// re-arms a second timer before any loss is declared.
func TestRACKReorderTimerDeclaresLossOnFirstExpiry(t *testing.T) {
	clock := faketime.NewManualClock()
	ctx := newRACKTestContext(clock, testClockResolution)
	defer ctx.cleanup()
	ctx.snd.ep.mu.Lock()
	defer ctx.snd.ep.mu.Unlock()

	// A newer packet was transmitted one tick after the older packet below,
	// and its ACK was received in the same clock bucket, so RACK.RTT and
	// RACK.ReoWnd are zero. The older packet's loss deadline is therefore
	// exactly one clock resolution past the ACK processing time.
	ctx.snd.rc.XmitTime = tcpip.MonotonicTime{}.Add(testClockResolution)
	ctx.snd.rc.EndSequence = 2
	older := ctx.addSegment(0, tcpip.MonotonicTime{})

	var timerFired bool
	ctx.snd.reorderTimer.cleanup()
	ctx.snd.reorderTimer.init(clock, func() { timerFired = true })

	// detectLoss must not declare loss while the uncertainty boundary has
	// not elapsed, and must arm the timer through that boundary so that the
	// first expiry observes a negative timeRemaining.
	if got := ctx.snd.rc.detectLoss(tcpip.MonotonicTime{}); got != 0 {
		t.Fatalf("detectLoss got %d losses, want 0", got)
	}
	if older.lost {
		t.Fatal("segment marked lost before the uncertainty boundary elapsed")
	}
	if !ctx.snd.reorderTimer.enabled() {
		t.Fatal("reorder timer not armed")
	}
	if got, want := ctx.snd.reorderTimer.target, (tcpip.MonotonicTime{}).Add(2*testClockResolution); got != want {
		t.Fatalf("reorder timer target = %v, want %v", got, want)
	}

	// A single expiry at the target must be sufficient to declare loss.
	clock.Advance(2 * testClockResolution)
	if !timerFired {
		t.Fatal("reorder timer did not fire at its target")
	}
	ctx.snd.FastRecovery.Active = true
	ctx.snd.SndNxt = 1
	ctx.snd.SndCwnd = 0
	if err := ctx.snd.rc.reorderTimerExpired(); err != nil {
		t.Fatalf("reorderTimerExpired failed: %v", err)
	}
	if !older.lost {
		t.Fatal("reorderTimerExpired did not declare loss on the first timer expiry")
	}
}

// TestRACKInitialDeliverySequence starts transmissions at the clock origin.
// An unset delivery timestamp must not compare their sequence numbers with zero.
func TestRACKInitialDeliverySequence(t *testing.T) {
	for _, test := range []struct {
		name string
		iss  seqnum.Value
	}{
		{name: "low_isn", iss: 1234},
		{name: "high_isn", iss: 1 << 31},
		{name: "sequence_wrap", iss: ^seqnum.Value(0) - 2},
	} {
		t.Run(test.name, func(t *testing.T) {
			clock := faketime.NewManualClock()
			ctx := newRACKTestContext(clock, 0)
			defer ctx.cleanup()
			ctx.snd.ep.mu.Lock()
			defer ctx.snd.ep.mu.Unlock()
			// Initialize the same connection-relative state as initSender.
			ctx.snd.rc.init(&ctx.snd, test.iss)
			ctx.addSegment(test.iss.Add(1), clock.NowMonotonic())
			later := ctx.addSegment(test.iss.Add(2), clock.NowMonotonic())

			clock.Advance(time.Millisecond)
			if got, want := ctx.snd.rc.detectLoss(clock.NowMonotonic()), 0; got != want {
				t.Errorf("losses before any delivery = %d, want %d", got, want)
			}

			// The first delivered segment also has transmission timestamp zero.
			// It must advance the marker, including when its end wraps to zero.
			ack := newOutgoingSegment(stack.TransportEndpointID{}, clock, buffer.Buffer{}, 0)
			defer ack.DecRef()
			ctx.snd.rc.update(later, ack)
			if got, want := ctx.snd.rc.EndSequence, test.iss.Add(3); got != want {
				t.Errorf("first delivery end = %d, want %d", got, want)
			}
		})
	}
}

func TestRACKInitialDSACKWindow(t *testing.T) {
	for _, test := range []struct {
		name string
		iss  seqnum.Value
	}{
		{name: "low_isn", iss: 1234},
		{name: "high_isn", iss: 1 << 31},
		{name: "sequence_wrap", iss: ^seqnum.Value(0) - 2},
	} {
		t.Run(test.name, func(t *testing.T) {
			ctx := newRACKTestContext(faketime.NewManualClock(), 0)
			defer ctx.cleanup()
			ctx.snd.ep.mu.Lock()
			defer ctx.snd.ep.mu.Unlock()
			ctx.snd.rc.init(&ctx.snd, test.iss)
			ctx.snd.SndUna = test.iss.Add(1)
			ctx.snd.SndNxt = test.iss.Add(4)
			ctx.snd.rc.minRTT = 100 * time.Millisecond
			ctx.snd.rtt.Lock()
			ctx.snd.rtt.TCPRTTState.SRTT = 100 * time.Millisecond
			ctx.snd.rtt.Unlock()

			// The first DSACK must expand the reordering window regardless
			// of where the connection starts in sequence space.
			ctx.snd.rc.DSACKSeen = true
			ctx.snd.rc.updateRACKReorderWindow()
			if got, want := ctx.snd.rc.ReoWnd, 50*time.Millisecond; got != want {
				t.Errorf("first DSACK window = %s, want %s", got, want)
			}
			if got, want := ctx.snd.rc.RTTSeq, ctx.snd.SndNxt; got != want {
				t.Errorf("DSACK round boundary = %d, want %d", got, want)
			}

			// Another DSACK in the same round cannot expand it again.
			ctx.snd.rc.DSACKSeen = true
			ctx.snd.rc.updateRACKReorderWindow()
			if got, want := ctx.snd.rc.ReoWnd, 50*time.Millisecond; got != want {
				t.Errorf("same-round DSACK window = %s, want %s", got, want)
			}

			// Acknowledging the round boundary allows a new expansion,
			// including when that boundary wraps through zero.
			ctx.snd.SndUna = ctx.snd.SndNxt
			ctx.snd.rc.DSACKSeen = true
			ctx.snd.rc.updateRACKReorderWindow()
			if got, want := ctx.snd.rc.ReoWnd, 75*time.Millisecond; got != want {
				t.Errorf("next-round DSACK window = %s, want %s", got, want)
			}
		})
	}
}

func TestRACKEqualTransmitTimeOrdersByEndSequence(t *testing.T) {
	clock := faketime.NewManualClock()
	ctx := newRACKTestContext(clock, 0)
	defer ctx.cleanup()
	ctx.snd.ep.mu.Lock()
	defer ctx.snd.ep.mu.Unlock()
	ctx.snd.rc.EndSequence = 2
	for seq := seqnum.Value(0); seq < 3; seq++ {
		ctx.addSegment(seq, tcpip.MonotonicTime{})
	}

	if got := ctx.snd.rc.detectLoss(tcpip.MonotonicTime{}); got != 1 {
		t.Fatalf("detectLoss got %d losses, want 1", got)
	}
	for seg := ctx.snd.writeList.Front(); seg != nil; seg = seg.Next() {
		wantLost := seg.sequenceNumber == 0
		if seg.lost != wantLost {
			t.Errorf("segment ending at %d lost=%t, want %t", seg.sequenceNumber.Add(1), seg.lost, wantLost)
		}
	}
}

func TestRACKDetectLossSteadyStateDoesNotAllocate(t *testing.T) {
	clock := faketime.NewManualClock()
	ctx := newRACKTestContext(clock, testClockResolution)
	defer ctx.cleanup()
	ctx.snd.ep.mu.Lock()
	ctx.snd.rc.XmitTime = tcpip.MonotonicTime{}.Add(testClockResolution)
	ctx.snd.rc.EndSequence = 2
	ctx.addSegment(0, tcpip.MonotonicTime{})
	ctx.snd.ep.mu.Unlock()

	// Exclude the existing timer facility's one-time initialization.
	ctx.snd.reorderTimer.enable(testClockResolution)
	allocs := testing.AllocsPerRun(1000, func() {
		ctx.snd.ep.mu.Lock()
		defer ctx.snd.ep.mu.Unlock()
		if got := ctx.snd.rc.detectLoss(tcpip.MonotonicTime{}.Add(testClockResolution)); got != 0 {
			panic("detectLoss unexpectedly reported loss")
		}
	})
	if allocs != 0 {
		t.Errorf("detectLoss allocated %v objects per call, want 0", allocs)
	}
}

func TestRACKZeroClockResolutionPreservesLossBoundary(t *testing.T) {
	clock := faketime.NewManualClock()
	ctx := newRACKTestContext(clock, 0)
	defer ctx.cleanup()
	ctx.snd.ep.mu.Lock()
	defer ctx.snd.ep.mu.Unlock()
	ctx.snd.rc.XmitTime = tcpip.MonotonicTime{}.Add(time.Nanosecond)
	ctx.snd.rc.EndSequence = 2
	ctx.addSegment(0, tcpip.MonotonicTime{})

	if got := ctx.snd.rc.detectLoss(tcpip.MonotonicTime{}); got != 1 {
		t.Fatalf("detectLoss at zero-resolution boundary got %d losses, want 1", got)
	}
}

// TestSACKCreditMSSAndSplit keeps credited entries beyond a rewound send cursor.
// MSS changes and queue fragmentation must conserve that credit.
func TestSACKCreditMSSAndSplit(t *testing.T) {
	clock := faketime.NewManualClock()
	ctx := newRACKTestContext(clock, testClockResolution)
	defer ctx.cleanup()
	s := &ctx.snd
	s.ep.mu.Lock()
	defer s.ep.mu.Unlock()
	defer s.updateWriteNext(nil)
	s.MaxPayloadSize = 10
	s.SndWnd = 100
	s.SndNxt = 40
	s.Outstanding = 1
	s.SackedOut = 3
	s.FastRecovery.Active = true
	s.state = tcpip.SACKRecovery
	s.ep.SACKPermitted = true
	s.ep.scoreboard.smss = 10
	// Keep retransmission disabled; this test exercises the rare MSS update
	// and split owners, while the packet fixture covers recovery traffic.
	s.SndCwnd = 0
	for _, entry := range []struct {
		seq   seqnum.Value
		size  int
		acked bool
	}{
		{seq: 0, size: 10},
		{seq: 10, size: 30, acked: true},
	} {
		seg := newOutgoingSegment(stack.TransportEndpointID{}, clock, buffer.MakeWithView(buffer.NewViewSize(entry.size)), 0)
		seg.sequenceNumber = entry.seq
		seg.flags = header.TCPFlagAck
		seg.xmitCount = 1
		seg.acked = entry.acked
		seg.lost = entry.acked
		s.writeList.PushBack(seg)
		ctx.segs = append(ctx.segs, seg)
	}
	s.ep.scoreboard.Insert(header.SACKBlock{Start: 10, End: 40})
	s.updateWriteNext(s.writeList.Front())

	for _, test := range []struct {
		mss   int
		count int
		want  int
	}{
		{mss: 5, count: 0, want: 6},
		{mss: 3, count: 1, want: 10},
	} {
		s.updateMaxPayloadSize(header.TCPMinimumSize+s.ep.maxOptionSize()+test.mss, test.count)
		if got, want := s.SackedOut, test.want; got != want {
			t.Errorf("SackedOut at MSS %d = %d, want %d", test.mss, got, want)
		}
	}

	credited := s.writeList.Front().Next()
	s.splitSeg(credited, 8)
	remainder := credited.Next()
	if remainder == nil {
		t.Fatal("split did not create a remainder")
	}
	ctx.segs = append(ctx.segs, remainder)
	for _, seg := range []*segment{credited, remainder} {
		if got, want := seg.acked, true; got != want {
			t.Errorf("split SACK credit = %t, want %t", got, want)
		}
		if got, want := seg.lost, true; got != want {
			t.Errorf("split loss state = %t, want %t", got, want)
		}
		if got, want := seg.flags&header.TCPFlagPsh, header.TCPFlags(0); got != want {
			t.Errorf("split PSH = %v, want %v", got, want)
		}
	}
	if got, want := s.SackedOut, 11; got != want {
		t.Errorf("SackedOut after split = %d, want %d", got, want)
	}
}

// TestRACKPipeCopyAccounting checks the accounting before any new send can
// consume a window. The two outstanding copies represent a retransmission of
// an original that had not yet been declared lost.
func TestRACKPipeCopyAccounting(t *testing.T) {
	clock := faketime.NewManualClock()
	ctx := newRACKTestContext(clock, 0)
	defer ctx.cleanup()
	s := &ctx.snd
	s.ep.mu.Lock()
	defer s.ep.mu.Unlock()
	const mss = 100
	const start seqnum.Value = 0xffffff70 // Include an actual sequence wrap.
	s.ep.scoreboard = NewSACKScoreboard(mss, start)
	s.ep.SACKPermitted = true
	s.ep.tcpRecovery = tcpip.TCPRACKLossDetection
	s.MaxPayloadSize = mss
	s.FastRecovery.Active = true
	s.SndUna, s.SndNxt = start, start.Add(250)
	seg := newOutgoingSegment(stack.TransportEndpointID{}, clock, buffer.MakeWithView(buffer.NewViewSize(250)), 0)
	seg.flags = header.TCPFlagAck
	seg.sequenceNumber = start
	seg.xmitCount, seg.rackInFlight = 2, 2
	seg.xmitTime = clock.NowMonotonic()
	s.writeList.PushBack(seg)
	defer func() {
		for x := s.writeList.Front(); x != nil; x = s.writeList.Front() {
			s.writeList.Remove(x)
			x.DecRef()
		}
	}()
	pipe := func(want int) {
		t.Helper()
		s.SetPipe()
		if s.Outstanding != want {
			t.Fatalf("flight = %d, want %d", s.Outstanding, want)
		}
	}
	pipe(6) // Two copies of three on-wire packets (including the short tail).
	s.splitSeg(seg, mss)
	middle := seg.Next()
	s.splitSeg(middle, mss)
	tail := middle.Next()
	if tail == nil || tail.payloadSize() != 50 {
		t.Fatalf("missing split GSO tail")
	}
	pipe(6)
	s.ep.scoreboard.Insert(header.SACKBlock{Start: start.Add(125), End: start.Add(150)})
	pipe(6)
	if s.prr.pipe != 450 {
		t.Fatalf("byte flight with 25 bytes SACKed inside a split packet = %d, want 450", s.prr.pipe)
	}
	s.ep.scoreboard.Insert(middle.sackBlock())
	pipe(4) // Delivery cancels both copies of just this middle SMSS.
	clock.Advance(time.Millisecond)
	s.rc.XmitTime, s.rc.EndSequence = clock.NowMonotonic(), s.SndNxt
	// The tail's current copy is newer than the delivered reference.
	tail.xmitTime = clock.NowMonotonic().Add(time.Millisecond)
	if n := s.rc.detectLoss(clock.NowMonotonic()); n != 1 || !seg.lost || seg.rackInFlight != 0 {
		t.Fatalf("lost retransmission: marked=%d lost=%t copies=%d", n, seg.lost, seg.rackInFlight)
	}
	pipe(2)
	s.rc.detectLoss(clock.NowMonotonic())
	pipe(2) // Repeated detection does not subtract another packet.
	s.ep.scoreboard.Insert(tail.sackBlock())
	pipe(0)
}

func TestPRRDeliveryBudget(t *testing.T) {
	ctx := newRACKTestContext(faketime.NewManualClock(), 0)
	defer ctx.cleanup()
	s := &ctx.snd
	s.ep.mu.Lock()
	defer s.ep.mu.Unlock()
	s.MaxPayloadSize = 100
	s.Ssthresh = 6
	s.prr = prrState{recoverFS: 1000, pipe: 800, delivered: 200, sent: 100}
	s.updatePRR(100, false, false)
	if got := s.prr.remaining; got != 80 {
		t.Fatalf("proportional allowance = %d, want 80", got)
	}
	s.updatePRR(0, false, false)
	if got := s.prr.remaining; got != 80 {
		t.Fatalf("repeated ACK allowance = %d, want 80", got)
	}
	s.updatePRR(200, false, false)
	if got := s.prr.remaining; got != 200 {
		t.Fatalf("cumulative proportional allowance = %d, want 200", got)
	}
	s.prr.pipe, s.prr.sent = 250, 500
	s.updatePRR(100, true, false)
	if got := s.prr.remaining; got != 200 {
		t.Fatalf("safe ACK allowance = %d, want 200", got)
	}
	s.prr.pipe, s.prr.sent = 560, 700
	s.updatePRR(100, true, false)
	if got := s.prr.remaining; got != 40 {
		t.Fatalf("target flight cap = %d, want 40", got)
	}
}
