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

package tcp_rack_test

import (
	"bytes"
	"slices"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/faketime"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/seqnum"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp/test/e2e"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp/testing/context"
)

// This research model is not a TCP implementation. It isolates ownership of
// byte coverage and current packetization from transmission-attempt history.
// Loss decisions are explicit inputs, not an implementation of RACK's timers.
// All views are recomputed; no caller can increment or decrement a counter.
// The attempt log is an unbounded research trace, not a lifetime-storage design.
// Pipe uses the RFC6675 HighRxt approximation, not a count of live attempts.
type deliveryModelRange struct{ start, end uint64 }

type deliveryModelAttempt struct {
	span deliveryModelRange
	mss  uint64
}

type deliveryModel struct {
	firstSeq    uint32
	una, next   uint64
	mss         uint64
	extents     []deliveryModelRange
	sacks, lost []deliveryModelRange
	attempts    []deliveryModelAttempt
	highRxtEnd  uint64
}

// Cumulative progress and new ACK knowledge differ when a later ACK retires
// bytes already known from SACK. Resetting advisory SACK state can cause this
// knowledge to be counted again; it is not a unique wire-delivery counter.
type deliveryModelACK struct {
	cumulativeBytes, newACKKnowledgeBytes uint64
}

type deliveryModelView struct {
	unackedBytes, sackedBytes, pipeBytes uint64
	projectedPackets, sackedPackets      uint64
	attempts                             int
}

func deliveryModelMerge(spans []deliveryModelRange) []deliveryModelRange {
	slices.SortFunc(spans, func(a, b deliveryModelRange) int {
		if a.start < b.start {
			return -1
		}
		if a.start > b.start {
			return 1
		}
		return 0
	})
	out := spans[:0]
	for _, span := range spans {
		if len(out) != 0 && span.start <= out[len(out)-1].end {
			out[len(out)-1].end = max(out[len(out)-1].end, span.end)
		} else {
			out = append(out, span)
		}
	}
	return out
}

func deliveryModelCovered(spans []deliveryModelRange, start, end uint64) bool {
	for _, span := range spans {
		if span.start <= start && end <= span.end {
			return true
		}
	}
	return false
}

func (m *deliveryModel) send(length uint64) {
	if length == 0 || length >= 1<<31 || m.next-m.una+length >= 1<<31 {
		panic("send exceeds modular flight window")
	}
	span := deliveryModelRange{m.next, m.next + length}
	m.extents = append(m.extents, span)
	m.attempts = append(m.attempts, deliveryModelAttempt{span: span, mss: m.mss})
	m.next = span.end
}

func (m *deliveryModel) split(at uint64) {
	for i, span := range m.extents {
		if span.start < at && at < span.end {
			m.extents = slices.Insert(m.extents, i+1, deliveryModelRange{at, span.end})
			m.extents[i].end = at
			return
		}
	}
}

// normalizeProjection retains the existing conservative full-MSS SACK policy.
// It partitions metadata only; payload and send attempts are not duplicated.
func (m *deliveryModel) normalizeProjection() {
	for _, sack := range m.sacks {
		for _, span := range slices.Clone(m.extents) {
			start, end := span.start, span.end
			if sack.start > start {
				start += (sack.start - start + m.mss - 1) / m.mss * m.mss
			}
			if sack.end < end {
				if sack.end <= span.start {
					continue
				}
				end = span.start + (sack.end-span.start)/m.mss*m.mss
			}
			if start < end && start < span.end {
				m.split(start)
				m.split(end)
			}
		}
	}
}

func (m *deliveryModel) ack(cumulative uint64, sacks ...deliveryModelRange) deliveryModelACK {
	previousSACKBytes := m.view().sackedBytes
	if cumulative < m.una || cumulative > m.next {
		panic("invalid cumulative ACK")
	}
	for _, sack := range sacks {
		if sack.start < sack.end && sack.start >= m.una && sack.end <= m.next {
			m.sacks = append(m.sacks, sack)
		}
	}
	m.sacks = deliveryModelMerge(m.sacks)
	// Normalize against the pre-retirement geometry, as the current sender
	// does. The byte union remains authoritative across this operation.
	m.normalizeProjection()
	progress := cumulative - m.una
	m.una = cumulative
	clip := func(spans []deliveryModelRange) []deliveryModelRange {
		out := spans[:0]
		for _, span := range spans {
			if span.end > cumulative {
				out = append(out, deliveryModelRange{max(span.start, cumulative), span.end})
			}
		}
		return out
	}
	m.extents = clip(m.extents)
	m.sacks = clip(m.sacks)
	m.lost = clip(m.lost)
	return deliveryModelACK{cumulativeBytes: progress, newACKKnowledgeBytes: progress + m.view().sackedBytes - previousSACKBytes}
}

func (m *deliveryModel) updateMSS(mss uint64) {
	if mss == 0 {
		panic("zero MSS")
	}
	m.mss = mss
}

func (m *deliveryModel) markLost(span deliveryModelRange) {
	if span.start < m.una || span.end > m.next || span.start >= span.end {
		panic("loss range outside sent data")
	}
	m.lost = deliveryModelMerge(append(m.lost, span))
}

func (m *deliveryModel) retransmit(span deliveryModelRange) {
	m.attempts = append(m.attempts, deliveryModelAttempt{span: span, mss: m.mss})
	m.highRxtEnd = max(m.highRxtEnd, span.end)
}

// resetSACK models the common advisory-data invalidation required by RTO and
// restore. Recovery policy and payload retention are separate from this step.
func (m *deliveryModel) resetSACK() { m.sacks = nil }

// offset validates modular input relative to the current cumulative frontier.
// Negative/old sequence numbers, including DSACKs, are not retained as SACKs.
func (m *deliveryModel) offset(sequence uint32) (uint64, bool) {
	delta := int32(sequence - (m.firstSeq + uint32(m.una)))
	if delta < 0 || uint64(delta) > m.next-m.una {
		return 0, false
	}
	return m.una + uint64(delta), true
}

func (m *deliveryModel) view() deliveryModelView {
	v := deliveryModelView{unackedBytes: m.next - m.una, attempts: len(m.attempts)}
	points := []uint64{m.una, m.next}
	for _, span := range m.extents {
		count := (span.end - span.start + m.mss - 1) / m.mss
		v.projectedPackets += count
		if deliveryModelCovered(m.sacks, span.start, span.end) {
			v.sackedPackets += count
		}
	}
	for _, spans := range [][]deliveryModelRange{m.sacks, m.lost} {
		for _, span := range spans {
			points = append(points, span.start, span.end)
		}
	}
	if m.una < m.highRxtEnd && m.highRxtEnd < m.next {
		points = append(points, m.highRxtEnd)
	}
	slices.Sort(points)
	points = slices.Compact(points)
	for i := 1; i < len(points); i++ {
		start, end := points[i-1], points[i]
		if start < m.una || end > m.next {
			continue
		}
		if deliveryModelCovered(m.sacks, start, end) {
			v.sackedBytes += end - start
			continue
		}
		// RFC6675 has two independent contributions. Retransmitting data
		// not yet considered lost can count an additional copy in pipe.
		if !deliveryModelCovered(m.lost, start, end) {
			v.pipeBytes += end - start
		}
		if end <= m.highRxtEnd {
			v.pipeBytes += end - start
		}
	}
	return v
}

func TestDeliveryOwnershipModel(t *testing.T) {
	t.Run("recovery_then_cumulative_ack", func(t *testing.T) {
		m := deliveryModel{mss: 10}
		for range 5 {
			m.send(10)
		}
		m.ack(10, deliveryModelRange{20, 50})
		if got, want := m.view(), (deliveryModelView{unackedBytes: 40, sackedBytes: 30, pipeBytes: 10, projectedPackets: 4, sackedPackets: 3, attempts: 5}); got != want {
			t.Fatalf("before loss = %+v, want %+v", got, want)
		}
		m.markLost(deliveryModelRange{10, 20})
		if got, want := m.view().pipeBytes, uint64(0); got != want {
			t.Fatalf("lost gap pipe = %d, want %d", got, want)
		}
		m.retransmit(deliveryModelRange{10, 20})
		if got, want := m.view().pipeBytes, uint64(10); got != want {
			t.Fatalf("retransmitted gap pipe = %d, want %d", got, want)
		}
		if got, want := m.ack(50), (deliveryModelACK{cumulativeBytes: 40, newACKKnowledgeBytes: 10}); got != want {
			t.Errorf("explicit ACK progress = %+v, want %+v", got, want)
		}
		if got, want := m.view(), (deliveryModelView{attempts: 6}); got != want {
			t.Errorf("retired flight = %+v, want %+v", got, want)
		}
	})
	t.Run("packet_projection_is_not_transmission_history", func(t *testing.T) {
		m := deliveryModel{mss: 10}
		m.send(30)
		m.ack(0, deliveryModelRange{0, 30})
		m.updateMSS(3)
		if got, want := m.view().sackedPackets, uint64(10); got != want {
			t.Fatalf("rebased credit = %d, want %d", got, want)
		}
		m.split(8)
		if got, want := m.view(), (deliveryModelView{unackedBytes: 30, sackedBytes: 30, projectedPackets: 11, sackedPackets: 11, attempts: 1}); got != want {
			t.Fatalf("split projection = %+v, want %+v", got, want)
		}
		m.resetSACK()
		if got, want := m.view(), (deliveryModelView{unackedBytes: 30, pipeBytes: 30, projectedPackets: 11, attempts: 1}); got != want {
			t.Errorf("reset retains data = %+v, want %+v", got, want)
		}
	})
	t.Run("overlap_and_simultaneous_retirement", func(t *testing.T) {
		m := deliveryModel{mss: 10}
		m.send(40)
		m.ack(0, deliveryModelRange{5, 15})
		m.ack(10, deliveryModelRange{10, 25})
		if got, want := m.view(), (deliveryModelView{unackedBytes: 30, sackedBytes: 15, pipeBytes: 15, projectedPackets: 3, sackedPackets: 1, attempts: 1}); got != want {
			t.Errorf("merged and clipped = %+v, want %+v", got, want)
		}
	})
	t.Run("partial_trim_order_is_an_explicit_projection_policy", func(t *testing.T) {
		sackFirst := deliveryModel{mss: 10}
		sackFirst.send(40)
		sackFirst.ack(0, deliveryModelRange{10, 30})
		sackFirst.ack(5)
		trimFirst := deliveryModel{mss: 10}
		trimFirst.send(40)
		trimFirst.ack(5)
		trimFirst.ack(5, deliveryModelRange{10, 30})
		if got, want := sackFirst.view().sackedBytes, trimFirst.view().sackedBytes; got != want {
			t.Errorf("byte coverage depends on order: %d, want %d", got, want)
		}
		if got, want := sackFirst.view().sackedPackets, uint64(2); got != want {
			t.Errorf("SACK before trim projection = %d, want %d", got, want)
		}
		if got, want := trimFirst.view().sackedPackets, uint64(1); got != want {
			t.Errorf("trim before SACK projection = %d, want %d", got, want)
		}
	})
	t.Run("two_copies_in_pipe", func(t *testing.T) {
		m := deliveryModel{mss: 10}
		m.send(20)
		m.retransmit(deliveryModelRange{0, 20})
		if got, want := m.view().pipeBytes, uint64(40); got != want {
			t.Errorf("original plus retransmission = %d, want %d", got, want)
		}
	})
	t.Run("modular_input_uses_current_frontier", func(t *testing.T) {
		m := deliveryModel{firstSeq: ^uint32(0) - 20, mss: 10}
		m.send(60)
		m.ack(30)
		if got, ok := m.offset(m.firstSeq + 40); !ok || got != 40 {
			t.Errorf("wrapped offset = %d, %t; want 40, true", got, ok)
		}
		_, ok := m.offset(m.firstSeq + 20)
		if got, want := ok, false; got != want {
			t.Errorf("old sequence accepted = %t, want %t", got, want)
		}
	})
}

// TestDeliverySenderReplay gives the model and the real TCP sender the same ACK
// events. Expected credit comes from the model's retained byte ranges, not a
// table of assigned sender counters. Timing and recovery policy are unchanged.
func TestDeliverySenderReplay(t *testing.T) {
	type ackEvent struct {
		cumulative uint64
		sacks      []deliveryModelRange
	}
	for _, test := range []struct {
		name   string
		gso    stack.SupportedGSO
		events []ackEvent
	}{
		{
			name: "disjoint_packets",
			events: []ackEvent{
				{cumulative: 10},
				{cumulative: 10, sacks: []deliveryModelRange{{40, 50}, {20, 30}}},
				{cumulative: 30},
				{cumulative: 50},
			},
		},
		{
			name: "merged_partial_sacks_with_retirement",
			gso:  stack.GVisorGSOSupported,
			events: []ackEvent{
				{cumulative: 10},
				{cumulative: 10, sacks: []deliveryModelRange{{20, 26}}},
				{cumulative: 25, sacks: []deliveryModelRange{{26, 30}}},
				{cumulative: 50},
			},
		},
		{
			name: "sack_then_partial_trim",
			gso:  stack.GVisorGSOSupported,
			events: []ackEvent{
				{cumulative: 10},
				{cumulative: 10, sacks: []deliveryModelRange{{20, 40}}},
				{cumulative: 15},
				{cumulative: 50},
			},
		},
		{
			name: "partial_trim_then_sack",
			gso:  stack.GVisorGSOSupported,
			events: []ackEvent{
				{cumulative: 10},
				{cumulative: 15},
				{cumulative: 15, sacks: []deliveryModelRange{{20, 40}}},
				{cumulative: 50},
			},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			clock := faketime.NewManualClock()
			clock.Advance(time.Second)
			states := make(chan *tcp.TCPEndpointState, 16)
			c := context.NewWithOpts(t, context.Options{
				EnableV4: true,
				MTU:      uint32(mtu),
				Clock:    clock,
				GSO:      test.gso,
				Probe:    func(state *tcp.TCPEndpointState) { states <- state },
			})
			defer c.Cleanup()
			e2e.SetStackSACKPermitted(t, c, true)
			e2e.SetStackTCPRecovery(t, c, int(tcpip.TCPRACKLossDetection))
			e2e.CreateConnectedWithSACKAndTS(c)
			data := make([]byte, 5*maxPayload)
			n, err := c.EP.Write(bytes.NewReader(data), tcpip.WriteOptions{})
			if err != nil {
				t.Fatalf("Write: %s", err)
			}
			if got, want := n, int64(len(data)); got != want {
				t.Fatalf("Write = %d, want %d", got, want)
			}
			model := deliveryModel{firstSeq: uint32(c.IRS.Add(1)), mss: maxPayload}
			for offset := 0; offset < len(data); offset += maxPayload {
				c.ReceiveAndCheckPacketWithOptions(data, offset, maxPayload, e2e.TSOptionSize)
				if test.gso == stack.GSONotSupported {
					model.send(maxPayload)
				}
			}
			if test.gso != stack.GSONotSupported {
				// Software GSO emits those packets from one queued extent.
				model.send(uint64(len(data)))
			}
			clock.Advance(100 * time.Millisecond)
			seq := seqnum.Value(context.TestInitialSequenceNumber).Add(1)
			type observation struct {
				event  ackEvent
				model  deliveryModelView
				actual *tcp.TCPEndpointState
			}
			var observations []observation
			for _, event := range test.events {
				var blocks []header.SACKBlock
				for _, span := range event.sacks {
					if span.start <= event.cumulative || span.start <= model.una || span.end > model.next {
						t.Fatalf("invalid replay SACK %+v at ACK %d", span, event.cumulative)
					}
					blocks = append(blocks, header.SACKBlock{
						Start: c.IRS.Add(1 + seqnum.Size(span.start)),
						End:   c.IRS.Add(1 + seqnum.Size(span.end)),
					})
				}
				c.SendAckWithSACK(seq, int(event.cumulative), blocks)
				model.ack(event.cumulative, event.sacks...)
				c.Stack().Pause()
				c.Stack().Resume()
				observations = append(observations, observation{event: event, model: model.view(), actual: <-states})
			}
			select {
			case state := <-states:
				t.Fatalf("unexpected probe snapshot after replay: UNA=%d NXT=%d", state.Sender.SndUna, state.Sender.SndNxt)
			default:
			}
			for index, observation := range observations {
				if got, want := observation.actual.Sender.SndUna, c.IRS.Add(1+seqnum.Size(observation.event.cumulative)); got != want {
					t.Errorf("event %d snapshot SndUna = %d, want %d", index, got, want)
				}
				if got, want := observation.actual.Sender.SndNxt, c.IRS.Add(1+seqnum.Size(len(data))); got != want {
					t.Errorf("event %d snapshot SndNxt = %d, want %d", index, got, want)
				}
				t.Logf("event %d ACK=%d SACK=%v model=%+v actual credit=%d outstanding=%d fastRecovery=%t", index, observation.event.cumulative, observation.event.sacks, observation.model, observation.actual.Sender.SackedOut, observation.actual.Sender.Outstanding, observation.actual.Sender.FastRecovery.Active)
				if got, want := observation.actual.Sender.SackedOut, int(observation.model.sackedPackets); got != want {
					t.Errorf("event %d SackedOut = %d, want model %d", index, got, want)
				}
				if got, want := uint64(uint32(observation.actual.Sender.SndNxt-observation.actual.Sender.SndUna)), observation.model.unackedBytes; got != want {
					t.Errorf("event %d unacknowledged bytes = %d, want model %d", index, got, want)
				}
			}
		})
	}
}
