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

package virtual_tcp_test

import (
	"reflect"
	"runtime"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/sync"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
)

type flightRange struct{ Start, End int32 }

type flightWire struct {
	Side                int
	Seq, End, Ack       int32
	Captured, Arrival   int64
	Flags               uint8
	Window              uint16
	TSVal, TSEcr        uint32
	SACK                []flightRange
	Payload             int
	PayloadHash         string
	Dropped, Retransmit bool
}

type flightEndpoint struct {
	Side                                            int
	Una, Nxt, RcvNxt, RcvAcc                        int32
	Window                                          uint32
	SRTT, RTTVar, RTO                               time.Duration
	MSS                                             int
	Cwnd, Ssthresh, Outstanding, Sacked, Duplicates int
	SndQueued, RcvQueued, RcvPending                int
	SACKSent, SACKReceived                          []flightRange
	FastRecovery                                    bool
	Timestamps, SACKEnabled                         bool
}

type flightAction struct {
	Path                                    string
	Range                                   *flightRange `json:",omitempty"`
	Una, Nxt                                int32
	Window                                  uint32
	Cwnd, Ssthresh, Outstanding, Duplicates int
	// Trigger is the most recent wire input currently being processed, if
	// any. A timer firing without an input has a nil Trigger.
	Trigger *flightWire
}

type flightEvent struct {
	Kind     string
	At       int64
	Wire     *flightWire     `json:",omitempty"`
	Endpoint *flightEndpoint `json:",omitempty"`
	Action   *flightAction   `json:",omitempty"`
}

type flightResult struct {
	Role                       string
	SenderMode, ReceiverMode   tcpip.TCPRecovery
	SenderLatchedMode          *tcpip.TCPRecovery
	ReferenceCut               int
	Fence                      uint32
	Match                      string
	SplitIndex                 int
	Expected, Actual           *flightEvent `json:",omitempty"`
	Originals, Holes           []flightWire
	Events                     []flightEvent
	End                        string
	EndNS                      int64
	EndAck                     uint32
	MaxProtectedWaitNS         int64
	StagedPackets              int
	ForwardRate, QueueCapacity int64
}

type flightRecorder struct {
	mu           sync.Mutex
	result       flightResult
	ref, compare *flightResult
	base         [2]uint32
	first        int64
	input        *flightWire
	original     int
}

func newFlight(role string, mode tcpip.TCPRecovery, ref, cmp *flightResult) *flightRecorder {
	return &flightRecorder{
		result: flightResult{Role: role, SenderMode: mode, ReceiverMode: tcpip.TCPRACKLossDetection, Match: "comparing"},
		ref:    ref, compare: cmp,
	}
}

func (f *flightRecorder) header(side int, flags header.TCPFlags, seq uint32, payload int, at int64) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if flags.Contains(header.TCPFlagSyn) {
		f.base[side] = seq + 1
	}
	if side == 0 && payload > 0 && f.first == 0 {
		f.first = at
	}
}

func (f *flightRecorder) ranges(blocks []header.SACKBlock, base uint32) []flightRange {
	var out []flightRange
	for _, b := range blocks {
		out = append(out, flightRange{int32(uint32(b.Start) - base), int32(uint32(b.End) - base)})
	}
	return out
}

func (f *flightRecorder) wire(p packetObservation) flightWire {
	s := p.From
	return flightWire{Side: s, Seq: int32(p.Sequence - f.base[s]), End: int32(p.EndSequence - f.base[s]), Ack: int32(p.Acknowledgment - f.base[1-s]),
		Captured: p.CapturedAtNS - f.first, Arrival: p.AtNS - f.first, Flags: p.Flags, Window: p.WireWindow, TSVal: p.WireTSVal, TSEcr: p.TSEcr,
		SACK: f.ranges(p.SACKBlocks, f.base[1-s]), Payload: p.PayloadBytes, PayloadHash: p.PayloadSHA256, Dropped: p.Dropped, Retransmit: p.Retransmitted}
}

// event is called with mu held. Exact matching includes all normalized wire,
// receive and sender state up to a witnessed recovery choice. An earlier
// unexplained input/state difference terminates the comparison.
func (f *flightRecorder) event(e flightEvent) {
	r := &f.result
	if f.first == 0 || r.End != "" || r.Match == "unmatched_prefix" || (f.ref == nil && r.ReferenceCut != 0) {
		return
	}
	if len(r.Events) >= 20000 {
		r.Match = "unmatched_prefix"
		r.End = "event_bound"
		return
	}
	i := len(r.Events)
	r.Events = append(r.Events, e)
	if f.compare == nil || r.Match != "comparing" {
		return
	}
	if i >= len(f.compare.Events) {
		r.Match = "unmatched_prefix"
		r.SplitIndex = i
		r.Actual = &e
		return
	}
	other := f.compare.Events[i]
	if sameEvent(e, other, r.Role == "sack_replay") {
		if f.result.Role == "rack_replay" && i+1 == f.compare.ReferenceCut {
			r.Match = "reference_prefix_matched"
		}
		return
	}
	r.SplitIndex = i
	r.Expected, r.Actual = &other, &e
	if r.Role == "sack_replay" && decisionSplit(e, other, r.Events[:i]) {
		r.Match = "matched_decision"
		return
	}
	r.Match = "unmatched_prefix"
}

// RACK alone updates SackedOut in walkSACK. Keep the diagnostic on both
// sides, but compare the actual SACK blocks and flight rather than this
// private count when comparing different implementations.
func sameEvent(a, b flightEvent, crossMode bool) bool {
	if crossMode && a.Endpoint != nil && b.Endpoint != nil {
		x, y := *a.Endpoint, *b.Endpoint
		x.Sacked, y.Sacked = 0, 0
		a.Endpoint, b.Endpoint = &x, &y
	}
	return reflect.DeepEqual(a, b)
}

func actualChoice(p string) bool {
	switch p {
	case "rack_mark_lost", "retransmit", "rto", "tlp_sent", "tlp_recovery", "rack_reordering_timer", "rack_on_ack", "three_duplicate_acks", "sack_scoreboard":
		return true
	}
	return false
}

// The common ACK entry includes pre-choice sender state. An end marker
// explicitly observes that the opposite arm processed this input without
// the corresponding additional choice. Matching a handler dispatch is not
// an on-wire decision.
func decisionSplit(a, b flightEvent, prefix []flightEvent) bool {
	for _, pair := range [][2]flightEvent{{a, b}, {b, a}} {
		earlier, later := pair[0], pair[1]
		if earlier.Action == nil || earlier.Action.Trigger != nil || earlier.At >= later.At {
			continue
		}
		// Require an explicit timer callback. A retransmission without an
		// input can also be caused by an application write.
		switch earlier.Action.Path {
		case "rto", "rack_reordering_timer", "tlp_sent":
			return true
		}
	}
	if a.Action == nil || !actualChoice(a.Action.Path) {
		a, b = b, a
	}
	if a.Action == nil || !actualChoice(a.Action.Path) || a.Action.Trigger == nil {
		return false
	}
	if b.Action == nil || a.At != b.At || !reflect.DeepEqual(a.Action.Trigger, b.Action.Trigger) {
		return false
	}
	if b.Action.Path != "ack_end" && !actualChoice(b.Action.Path) {
		return false
	}
	if a.Action.Path == b.Action.Path && reflect.DeepEqual(a.Action.Range, b.Action.Range) {
		return false
	}
	for k := len(prefix) - 1; k >= 0; k-- {
		e := prefix[k]
		if e.Kind == "input" || e.At != a.At {
			return false
		}
		if e.Action != nil && e.Action.Path == "ack_begin" {
			return reflect.DeepEqual(e.Action.Trigger, a.Action.Trigger)
		}
	}
	return false
}

func (f *flightRecorder) endpoint(st *tcp.TCPEndpointState) {
	side := 0
	switch st.ID.LocalPort {
	case 40000:
	case 8080:
		side = 1
	default:
		return
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.first == 0 {
		return
	}
	s := st.Sender
	ep := flightEndpoint{Side: side, Una: int32(uint32(s.SndUna) - f.base[side]), Nxt: int32(uint32(s.SndNxt) - f.base[side]),
		RcvNxt: int32(uint32(st.Receiver.RcvNxt) - f.base[1-side]), RcvAcc: int32(uint32(st.Receiver.RcvAcc) - f.base[1-side]),
		Window: uint32(s.SndWnd), SRTT: s.RTTState.SRTT, RTTVar: s.RTTState.RTTVar, RTO: s.RTO, MSS: s.MaxPayloadSize, Cwnd: s.SndCwnd, Ssthresh: s.Ssthresh, Outstanding: s.Outstanding, Sacked: s.SackedOut, Duplicates: s.DupAckCount,
		SndQueued: st.SndBufState.SndBufUsed, RcvQueued: st.RcvBufState.RcvBufUsed, RcvPending: st.Receiver.PendingBufUsed,
		SACKSent: f.ranges(st.SACK.Blocks, f.base[1-side]), SACKReceived: f.ranges(st.SACK.ReceivedBlocks, f.base[side]), FastRecovery: s.FastRecovery.Active,
		Timestamps: st.SendTSOk, SACKEnabled: st.SACKPermitted}
	f.event(flightEvent{Kind: "peer", At: int64(st.SegTime.Sub(tcpip.MonotonicTime{})) - f.first, Endpoint: &ep})
}

func (f *flightRecorder) decision(d tcp.TCPRecoveryDecision) {
	if d.LocalPort != 40000 {
		return
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.first == 0 || f.result.End != "" {
		return
	}
	mode := d.Mode
	f.result.SenderLatchedMode = &mode
	if mode != f.result.SenderMode {
		f.result.Match = "unmatched_prefix"
		f.result.End = "latched_mode"
		return
	}
	a := flightAction{Path: d.Path, Una: int32(d.Una - f.base[0]), Nxt: int32(d.Nxt - f.base[0]), Window: d.Window,
		Cwnd: d.Cwnd, Ssthresh: d.Ssthresh, Outstanding: d.Outstanding, Duplicates: d.DupACKs, Trigger: f.input}
	if d.HasRange {
		a.Range = &flightRange{int32(d.Start - f.base[0]), int32(d.End - f.base[0])}
	}
	f.event(flightEvent{Kind: "decision", At: d.AtNS - f.first, Action: &a})
	if f.ref == nil && f.result.ReferenceCut == 0 && (d.Path == "three_duplicate_acks" || d.Path == "sack_scoreboard" || d.Path == "rack_on_ack" || d.Path == "rack_reordering_timer" || d.Path == "tlp_recovery" || d.Path == "rto") {
		f.result.ReferenceCut = len(f.result.Events)
		f.result.Fence = uint32(a.Nxt)
	}
}

func (f *flightRecorder) offer(p packetObservation) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.first == 0 || f.result.End != "" || (f.ref == nil && f.result.ReferenceCut != 0) {
		return
	}
	w := f.wire(p)
	if w.Side == 0 && w.Payload != 0 && !w.Retransmit && (f.ref == nil || w.Seq < int32(f.ref.Fence)) {
		f.result.Originals = append(f.result.Originals, w)
		if w.Dropped {
			f.result.Holes = append(f.result.Holes, w)
		}
		if f.ref != nil {
			if f.original >= len(f.ref.Originals) || !reflect.DeepEqual(w, f.ref.Originals[f.original]) {
				f.result.Match = "unmatched_prefix"
				f.result.End = "original_flight"
				f.result.SplitIndex = len(f.result.Events)
				f.result.Actual = &flightEvent{Kind: "offer", At: w.Captured, Wire: &w}
				if f.original < len(f.ref.Originals) {
					want := f.ref.Originals[f.original]
					f.result.Expected = &flightEvent{Kind: "offer", At: want.Captured, Wire: &want}
				}
			}
			f.original++
		}
	}
	f.event(flightEvent{Kind: "offer", At: w.Captured, Wire: &w})
}

func (f *flightRecorder) ingress(p packetObservation) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.first == 0 {
		return
	}
	w := f.wire(p)
	kind := "input"
	if w.Dropped {
		kind = "discard"
	} else {
		f.input = &w
	}
	f.event(flightEvent{Kind: kind, At: w.Arrival, Wire: &w})
}

func (f *flightRecorder) endInput() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.input = nil
}

// replayDrop takes only the frozen first-transmission holes out of the wire.
// Later packets retain forward serialization. When finite admission is full,
// they wait at its entrance until they fit; no retransmission is re-dropped.
func (f *flightRecorder) replayDrop(offset uint32, payload int, retransmit bool, now, serviceEnd, cost, capacity time.Duration) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	if !retransmit && offset < f.ref.Fence && f.original < len(f.ref.Originals) {
		want := f.ref.Originals[f.original]
		if uint32(want.Seq) == offset && int(want.End-want.Seq) == payload && want.Dropped {
			return true
		}
	}
	wait := max(time.Duration(0), max(now, serviceEnd)+cost-capacity-now)
	if wait > 0 {
		f.result.StagedPackets++
		f.result.MaxProtectedWaitNS = max(f.result.MaxProtectedWaitNS, int64(wait))
	}
	return false
}

func (f *flightRecorder) stop(now int64, ack uint32, capNS int64) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	r := &f.result
	switch {
	case r.Match == "unmatched_prefix":
		if r.End == "" {
			r.End = "unmatched_prefix"
		}
	case f.ref == nil && r.ReferenceCut != 0:
		r.End = "reference_first_recovery"
		r.Match = "reference"
		if len(r.Holes) == 0 || r.Holes[0].Seq != 335*1420 || r.Holes[0].Captured != 523572800 ||
			len(r.Originals) == 0 || uint32(r.Originals[len(r.Originals)-1].End) != r.Fence || r.Events[r.ReferenceCut-1].At != 726122240 ||
			r.Events[r.ReferenceCut-1].Action.Trigger == nil || r.Events[r.ReferenceCut-1].Action.Trigger.Ack != 335*1420 {
			r.Match = "unmatched_prefix"
			r.End = "historic_anchor_or_flight"
		}
	case f.ref != nil && ack >= f.ref.Fence:
		r.End = "flight_acknowledged"
		r.Fence = f.ref.Fence
	case now >= capNS:
		r.End = "absolute_cap"
		if f.ref != nil {
			r.Fence = f.ref.Fence
		} else {
			r.Match = "unmatched_prefix"
		}
	default:
		return false
	}
	if f.ref != nil && r.Match != "unmatched_prefix" {
		if f.original != len(f.ref.Originals) || !reflect.DeepEqual(r.Holes, f.ref.Holes) {
			r.Match = "unmatched_prefix"
			r.End = "incomplete_frozen_flight"
		}
		if r.Role == "sack_replay" && r.Match == "comparing" {
			if len(r.Events) == len(f.compare.Events) {
				r.Match = "no_observed_mode_divergence"
			} else {
				r.Match = "unmatched_prefix"
				r.End = "truncated_prefix"
			}
		}
	}
	r.EndNS, r.EndAck = now-f.first, ack
	return true
}

func TestVirtualTCPFrozenRecoveryFlight(t *testing.T) {
	if runtime.GOMAXPROCS(0) != 1 {
		t.Fatal("the manual clock fixture requires one TCP processor per stack")
	}
	run := func(role string, mode tcpip.TCPRecovery, ref, cmp *flightResult) *flightResult {
		f := newFlight(role, mode, ref, cmp)
		t.Run(role, func(t *testing.T) {
			runVirtualTCP(t, "cubic", nil, virtualScenario{delay: 50 * time.Millisecond, rate: 12500000, maxTime: 30 * time.Second, bytes: 16 << 20,
				dropOffset: -1, burstSegments: 1, streaming: true, startup: &startupPulse{LowerMSS: 30}, hyStartPlus: true, forwardQueue: &startupQueue{Rate: 1250000, Capacity: 125000}, flight: f})
		})
		return &f.result
	}
	ref := run("reference", tcpip.TCPRACKLossDetection, nil, nil)
	if ref.Match != "reference" {
		return
	}
	rack := run("rack_replay", tcpip.TCPRACKLossDetection, ref, ref)
	if rack.Match != "reference_prefix_matched" {
		return
	}
	run("sack_replay", 0, ref, rack)
}
