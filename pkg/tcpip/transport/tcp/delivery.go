// Copyright 2018 The gVisor Authors.
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
	"slices"

	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/seqnum"
)

// senderDelivery owns the queued sequence space and its delivery accounting.
// Access requires the owning sender's endpoint lock. Only delivery operations
// mutate these fields; snapshots expose copies to observers.
//
// +stateify savable
type senderDelivery struct {
	una, next     seqnum.Value
	mss           int
	flightPackets int
	sackedPackets int
	scoreboard    *SACKScoreboard
	queue         protectedWriteList
	writeNext     *segment
}

// snapshot copies the control and delivery views after an entire transition.
//
// +checklocks:s.ep.mu
func (s *sender) snapshot() TCPSenderState {
	return TCPSenderState{
		LastSendTime:                s.LastSendTime,
		DupAckCount:                 s.DupAckCount,
		SndCwnd:                     s.SndCwnd,
		Ssthresh:                    s.Ssthresh,
		SndCAAckCount:               s.SndCAAckCount,
		SndWnd:                      s.SndWnd,
		RTTMeasureSeqNum:            s.RTTMeasureSeqNum,
		RTTMeasureTime:              s.RTTMeasureTime,
		Closed:                      s.Closed,
		RTO:                         s.RTO,
		RTTState:                    s.RTTState,
		SndWndScale:                 s.SndWndScale,
		MaxSentAck:                  s.MaxSentAck,
		FastRecovery:                s.FastRecovery,
		Cubic:                       s.Cubic,
		RACKState:                   s.RACKState,
		RetransmitTS:                s.RetransmitTS,
		SpuriousRecovery:            s.SpuriousRecovery,
		Outstanding:                 s.delivery.flightPackets,
		SackedOut:                   s.delivery.sackedPackets,
		SndUna:                      s.delivery.una,
		SndNxt:                      s.delivery.next,
		MaxPayloadSize:              s.delivery.mss,
		UnacknowledgedSequenceBytes: s.delivery.unacknowledgedBytes(),
		SACKedBytes:                 s.delivery.sackedBytes(),
		CongestionState:             s.state,
	}
}

// resetSACK discards the scoreboard and the credits associated with its ranges.
func (d *senderDelivery) resetSACK() {
	d.scoreboard.Reset()
	d.sackedPackets = 0
	for seg := d.queue.Front(); seg != nil; seg = seg.Next() {
		seg.acked = false
	}
}

// packetCount returns the number of packets in the segment. Due to GSO, a segment
// can be composed of multiple packets.
func packetCount(seg *segment, maxPayloadSize int) int {
	size := seg.payloadSize()
	if size == 0 {
		return 1
	}

	return (size-1)/maxPayloadSize + 1
}

// split splits a given segment at the size specified and inserts the
// remainder as a new segment after the current one in the write list.
func (d *senderDelivery) split(seg *segment, size int) {
	if seg.payloadSize() <= size {
		return
	}
	// Split this segment up, preserving any existing selective ACK credit.
	oldPackets := packetCount(seg, d.mss)
	nSeg := seg.clone()
	nSeg.pkt.Data().TrimFront(size)
	nSeg.sequenceNumber.UpdateForward(seqnum.Size(size))
	d.queue.InsertAfter(seg, nSeg)

	// The segment being split does not carry PUSH flag because it is
	// followed by the newly split segment.
	// RFC1122 section 4.2.2.2: MUST set the PSH bit in the last buffered
	// segment (i.e., when there is no more queued data to be sent).
	// Linux removes PSH flag only when the segment is being split over MSS
	// and retains it when we are splitting the segment over lack of sender
	// window space.
	// ref: net/ipv4/tcp_output.c::tcp_write_xmit(), tcp_mss_split_point()
	// ref: net/ipv4/tcp_output.c::tcp_write_wakeup(), tcp_snd_wnd_test()
	if seg.payloadSize() > d.mss {
		seg.flags &^= header.TCPFlagPsh
	}
	seg.pkt.Data().CapLength(size)
	if seg.acked {
		d.sackedPackets += packetCount(seg, d.mss) + packetCount(nSeg, d.mss) - oldPackets
	}
}

func (d *senderDelivery) setNext(seg *segment) {
	if d.writeNext != nil {
		d.writeNext.DecRef()
	}
	if seg != nil {
		seg.IncRef()
	}
	d.writeNext = seg
}

// protectedWriteList wraps the write list, checking for invalid state when
// segments are added or removed.
//
// TODO(b/339664055): Revert once bug is fixed.
//
// +stateify savable
type protectedWriteList struct {
	writeList segmentList
	set       map[*segment]struct{}
}

// Front returns the front of the write list.
func (wl *protectedWriteList) Front() *segment {
	return wl.writeList.Front()
}

// Back returns the back of the write list.
func (wl *protectedWriteList) Back() *segment {
	return wl.writeList.Back()
}

// Remove removes seg from the write list.
func (wl *protectedWriteList) Remove(seg *segment) {
	if _, ok := wl.set[seg]; !ok {
		panic("segment not found write list")
	}
	wl.writeList.Remove(seg)
	delete(wl.set, seg)
}

// PushBack pushes seg onto the back of the write list.
func (wl *protectedWriteList) PushBack(seg *segment) {
	if _, ok := wl.set[seg]; ok {
		panic("segment already in write list")
	}
	wl.writeList.PushBack(seg)
	wl.set[seg] = struct{}{}
}

// InsertAfter inserts seg after before.
func (wl *protectedWriteList) InsertAfter(before, seg *segment) {
	if _, ok := wl.set[seg]; ok {
		panic("segment already in write list")
	}
	wl.writeList.InsertAfter(before, seg)
	wl.set[seg] = struct{}{}
}

// updateMSS changes packet projection and applies any PMTU loss report.
// The retransmission cursor may have been rewound before credited entries.
func (d *senderDelivery) updateMSS(mss, count int) bool {
	if mss >= d.mss {
		return false
	}
	mss = max(mss, 1)
	oldMSS := d.mss
	d.mss = mss
	if d.scoreboard != nil {
		d.scoreboard.smss = uint16(mss)
	}
	for seg := d.queue.Front(); seg != nil; seg = seg.Next() {
		if seg.acked {
			d.sackedPackets += packetCount(seg, mss) - packetCount(seg, oldMSS)
		}
	}
	if count == 0 {
		return true
	}
	d.flightPackets = max(d.flightPackets-count, 0)
	next := d.writeNext
	for seg := d.queue.Front(); seg != nil && seg != d.writeNext; seg = seg.Next() {
		if next == d.writeNext && seg.payloadSize() > mss {
			next = seg
		}
	}
	d.setNext(next)
	return true
}

// validSACKBlock checks whether a non-DSACK range is within the current flight.
func (d *senderDelivery) validSACKBlock(sb header.SACKBlock, ack seqnum.Value) bool {
	return ack.LessThan(sb.Start) && d.una.LessThan(sb.Start) && sb.Start.LessThan(sb.End) && sb.End.LessThanEq(d.next)
}

// applySACK updates retained byte coverage and its packet projection together.
// delivered observes newly credited extents before later retirement can release
// them. skip excludes a detected DSACK from delivery callbacks, not insertion.
func (d *senderDelivery) applySACK(ack seqnum.Value, blocks []header.SACKBlock, skip int, delivered func(deliverySample)) bool {
	newInfo := false
	for _, block := range blocks {
		if d.validSACKBlock(block, ack) && !d.scoreboard.IsSACKED(block) {
			d.scoreboard.Insert(block)
			newInfo = true
		}
	}
	n := len(blocks) - skip
	if n == 0 {
		return newInfo
	}
	// Sort the SACK blocks. The first block is the most recent unacked
	// block. The following blocks can be in arbitrary order.
	sackBlocks := make([]header.SACKBlock, 0, n)
	for _, sb := range blocks[skip:] {
		// Bound every incoming block to the current flight before lookup.
		// Ignore ranges that the scoreboard did not retain.
		if !d.validSACKBlock(sb, ack) {
			continue
		}
		if retained, ok := d.scoreboard.sackedBlock(sb); ok {
			// Overlapping partial blocks may cover an MSS only after merging.
			// This ACK may also make a prefix cumulative; retirement below
			// removes that prefix's credit from the retained range.
			sackBlocks = append(sackBlocks, retained)
		}
	}
	slices.SortFunc(sackBlocks, func(a, b header.SACKBlock) int {
		if a.Start.LessThan(b.Start) {
			return -1
		}
		if b.Start.LessThan(a.Start) {
			return 1
		}
		return 0
	})

	seg := d.queue.Front()
	for _, sb := range sackBlocks {
		for seg != nil && seg.sequenceNumber.LessThan(sb.End) && seg.xmitCount != 0 && seg.payloadSize() != 0 {
			if seg.acked || seg.sequenceNumber.Add(seqnum.Size(seg.payloadSize())).LessThanEq(sb.Start) {
				seg = seg.Next()
				continue
			}
			// A GSO segment can cover several wire packets. Split only at
			// MSS boundaries so the credited entry is entirely SACKed.
			// See Linux tcp_match_skb_to_sack:
			// https://github.com/torvalds/linux/blob/e5f0a698b/net/ipv4/tcp_input.c#L1334-L1388
			if seg.sequenceNumber.LessThan(sb.Start) {
				prefix := int(seg.sequenceNumber.Size(sb.Start))
				prefix = ((prefix-1)/d.mss + 1) * d.mss
				if prefix >= seg.payloadSize() {
					seg = seg.Next()
					continue
				}
				d.split(seg, prefix)
				seg = seg.Next()
				if !seg.sequenceNumber.LessThan(sb.End) {
					break
				}
			}
			if sb.End.LessThan(seg.sequenceNumber.Add(seqnum.Size(seg.payloadSize()))) {
				prefix := int(seg.sequenceNumber.Size(sb.End)) / d.mss * d.mss
				if prefix == 0 {
					break
				}
				d.split(seg, prefix)
			}
			if delivered != nil {
				delivered(deliveryOf(seg))
			}
			seg.acked = true
			d.sackedPackets += packetCount(seg, d.mss)
			seg = seg.Next()
		}
	}
	return newInfo
}

// deliveryACK reports cumulative sequence progress and the packet credit
// consumed by the existing congestion-control policy. Credit is explicit even
// when an RTO has reset the current flight budget below the retired projection.
type deliveryACK struct {
	bytes   seqnum.Size
	packets int
}

// retire advances the cumulative frontier and releases the corresponding
// queue ownership. pipeExcludedSACK records the accounting basis before an ACK
// changes recovery mode; it must not be inferred from the resulting mode.
func (d *senderDelivery) retire(ack seqnum.Value, pipeExcludedSACK bool, delivered func(deliverySample)) deliveryACK {
	progress := deliveryACK{bytes: d.una.Size(ack)}
	d.una = ack
	left := progress.bytes
	for left > 0 {
		seg := d.queue.Front()
		if seg == nil {
			panic(fmt.Sprintf("ACK retires %d bytes beyond the delivery queue", left))
		}
		length := seg.logicalLen()
		previous := packetCount(seg, d.mss)
		if length > left {
			seg.TrimFront(left)
			seg.sequenceNumber.UpdateForward(left)
			retired := previous - packetCount(seg, d.mss)
			if seg.acked {
				d.sackedPackets -= retired
			}
			if !seg.acked || !pipeExcludedSACK {
				d.flightPackets -= retired
				progress.packets += retired
			}
			break
		}
		if d.writeNext == seg {
			d.setNext(seg.Next())
		}
		if !seg.acked && delivered != nil {
			delivered(deliveryOf(seg))
		}
		d.queue.Remove(seg)
		if seg.acked {
			d.sackedPackets -= previous
		}
		if !seg.acked || !pipeExcludedSACK {
			d.flightPackets -= previous
			progress.packets += previous
		}
		seg.DecRef()
		left -= length
	}
	d.scoreboard.Delete(d.una)
	// RTO can reset the flight budget below the retired packet projection.
	// The explicit result preserves that ACK credit without exposing a
	// transient negative budget to controller code.
	d.flightPackets = max(d.flightPackets, 0)
	if d.una == d.next {
		d.flightPackets = 0
	}
	return progress
}

// setPipe projects the existing RFC6675 recovery estimate in packet units.
// It is not a count of live transmission attempts. The protocol owner calls it
// only while SACK recovery is active, matching the existing scan boundary.
func (d *senderDelivery) setPipe(highRxt seqnum.Value) {
	pipe := 0
	smss := seqnum.Size(d.scoreboard.SMSS())
	for s1 := d.queue.Front(); s1 != nil && s1.payloadSize() != 0 && s1.flags != 0; s1 = s1.Next() {
		// With GSO each segment can be much larger than SMSS. So check the segment
		// in SMSS sized ranges.
		segEnd := s1.sequenceNumber.Add(seqnum.Size(s1.payloadSize()))
		for startSeq := s1.sequenceNumber; startSeq.LessThan(segEnd); startSeq = startSeq.Add(smss) {
			endSeq := startSeq.Add(smss)
			if segEnd.LessThan(endSeq) {
				endSeq = segEnd
			}
			sb := header.SACKBlock{Start: startSeq, End: endSeq}
			// SetPipe():
			//
			// After initializing pipe to zero, the following steps are
			// taken for each octet 'S1' in the sequence space between
			// HighACK and HighData that has not been SACKed:
			if !s1.sequenceNumber.LessThan(d.next) {
				break
			}
			if d.scoreboard.IsSACKED(sb) {
				continue
			}

			// SetPipe():
			//
			//    (a) If IsLost(S1) returns false, Pipe is incremened by 1.
			//
			// NOTE: here we mark the whole segment as lost. We do not try
			// and test every byte in our write buffer as we maintain our
			// pipe in terms of outstanding packets and not bytes.
			if !d.scoreboard.IsRangeLost(sb) {
				pipe++
			}
			// SetPipe():
			//    (b) If S1 <= HighRxt, Pipe is incremented by 1.
			if s1.sequenceNumber.LessThanEq(highRxt) {
				pipe++
			}
		}
	}
	d.flightPackets = pipe
}

// transmissionAccounting preserves the existing roles of window sends,
// first fast retransmission, and repeated tail probes in the flight budget.
type transmissionAccounting uint8

const (
	transmissionInWindow        transmissionAccounting = iota
	transmissionAtRecoveryEntry                        // SetPipe accounts for this attempt afterward.
	transmissionTailProbe                              // The existing TLP policy does not charge a second copy.
)

// transmit records a sender-accounted attempt, including failed link writes.
// No connection-lifetime attempt history is retained: queued extents keep only
// their existing last-transmission metadata and retransmission count.
func (d *senderDelivery) transmit(seg *segment, now tcpip.MonotonicTime, accounting transmissionAccounting) {
	// TODO(b/379932042): Retain the queue-membership invariant while its
	// original failure is investigated. All transmit roles use this owner.
	if _, ok := d.queue.set[seg]; !ok {
		panic("attempted to send segment not in delivery queue")
	}
	seg.xmitTime = now
	seg.xmitCount++
	seg.lost = false
	if accounting == transmissionInWindow {
		d.flightPackets += packetCount(seg, d.mss)
	}
}

func (d *senderDelivery) advanceSent(end seqnum.Value) {
	if d.next.LessThan(end) {
		d.next = end
	}
}

func (d *senderDelivery) markLost(seg *segment)  { seg.lost = true }
func (d *senderDelivery) clearLost(seg *segment) { seg.lost = false }

// timeout invalidates advisory delivery knowledge and restarts the current
// transmission budget while retaining queued payload and transmission metadata.
func (d *senderDelivery) timeout() {
	d.flightPackets = 0
	d.resetSACK()
	d.setNext(d.queue.Front())
}

func newSenderDelivery(iss seqnum.Value, mss int) senderDelivery {
	return senderDelivery{
		una:   iss + 1,
		next:  iss + 1,
		mss:   mss,
		queue: protectedWriteList{set: make(map[*segment]struct{})},
	}
}

// initializeScoreboard follows the initial path-MTU adjustment. Restored
// senders retain their scoreboard allocation and use resetSACK instead.
func (d *senderDelivery) initializeScoreboard(iss seqnum.Value) {
	d.scoreboard = NewSACKScoreboard(uint16(d.mss), iss)
}

func (d *senderDelivery) enqueue(seg *segment) { d.queue.PushBack(seg) }

// mergeUnsent transfers payload ownership from an adjacent unsent extent.
func (d *senderDelivery) mergeUnsent(seg, next *segment) {
	seg.merge(next)
	d.queue.Remove(next)
	next.DecRef()
}

func (d *senderDelivery) assignSequence(seg *segment) {
	seg.sequenceNumber = d.next
	seg.flags = header.TCPFlagAck | header.TCPFlagPsh
}

// purge drops all queued ownership, including the cursor's extra reference.
func (d *senderDelivery) purge() {
	d.setNext(nil)
	for seg := d.queue.Front(); seg != nil; seg = d.queue.Front() {
		d.queue.Remove(seg)
		seg.DecRef()
	}
	d.next = d.una
	d.scoreboard.Reset()
	d.sackedPackets = 0
	d.flightPackets = 0
}

func (d *senderDelivery) unacknowledgedBytes() seqnum.Size { return d.una.Size(d.next) }
func (d *senderDelivery) sackedBytes() seqnum.Size         { return d.scoreboard.Sacked() }

func (d *senderDelivery) prepareFIN(seg *segment) seqnum.Value {
	if d.queue.Back() != seg {
		panic("FIN segments must be the final segment in the delivery queue")
	}
	seg.flags = header.TCPFlagAck | header.TCPFlagFin
	return seg.sequenceNumber.Add(1)
}

// deliverySample is the immutable transmission evidence a loss detector needs
// when bytes become acknowledged. It never lends the detector a mutable queue
// entry or a packet-buffer reference that retirement may release.
type deliverySample struct {
	end       seqnum.Value
	xmitTime  tcpip.MonotonicTime
	xmitCount uint32
}

func deliveryOf(seg *segment) deliverySample {
	return deliverySample{
		end:       seg.sequenceNumber.Add(seqnum.Size(seg.payloadSize())),
		xmitTime:  seg.xmitTime,
		xmitCount: seg.xmitCount,
	}
}
