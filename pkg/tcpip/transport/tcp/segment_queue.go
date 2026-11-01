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

import "gvisor.dev/gvisor/pkg/tcpip/header"

// segmentQueue is a bounded, thread-safe queue of TCP segments.
//
// +stateify savable
type segmentQueue struct {
	mu segmentQueueMutex `state:"nosave"`
	// +checklocks:mu
	list segmentList `state:"wait"`
	ep   *Endpoint
	// +checklocks:mu
	frozen bool
}

// emptyLocked is equivalent to empty with q.mu already held.
//
// +checklocks:q.mu
func (q *segmentQueue) emptyLocked() bool {
	return q.list.Empty()
}

// empty determines if the queue is empty.
//
// +checklocksexclude:q.mu
func (q *segmentQueue) empty() bool {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.emptyLocked()
}

// enqueue adds the given segment to the queue.
//
// If enqueue succeeds, the queue acquires its own reference and returns true.
// Otherwise it returns false. The caller retains its reference in either case.
//
// +checklocksexclude:q.mu
func (q *segmentQueue) enqueue(s *segment) bool {
	q.mu.Lock()
	defer q.mu.Unlock()
	if q.frozen {
		return false
	}

	over := int64(q.ep.receiveMemUsed()) - q.ep.ops.GetReceiveBufferSize()
	// Reserve room for ACKs when unread data fills the receive buffer.
	// Shrinking the buffers below current usage must still allow a packet
	// through an empty backlog, or the peer cannot acknowledge our sends.
	if !q.emptyLocked() && over > q.ep.ops.GetSendBufferSize() {
		return false
	}
	if over > 0 && s.payloadSize() != 0 {
		// Drop the payload but retain its headers so the receiver sends an
		// ACK with the current receive window (RFC 9293 section 3.10.7.4).
		// Handshake completion can requeue an already-owned segment. Release
		// its old charge before replacing the payload; setOwner below will
		// account for the compact packet.
		if s.ep != nil && s.qFlags == recvQ {
			s.ep.updateReceiveMemUsed(-s.segMemSize())
			s.ep = nil
			s.qFlags = 0
		}
		oldPkt := s.pkt
		oldPkt.Data().CapLength(0)
		// Truncating a view does not release its backing allocation. Copy the
		// remaining headers so the queued segment does not retain the payload.
		s.pkt = oldPkt.DeepCopyForForwarding(0)
		s.pkt.NICID = oldPkt.NICID
		s.pkt.NetworkPacketInfo = oldPkt.NetworkPacketInfo
		s.options = s.pkt.TransportHeader().Slice()[header.TCPMinimumSize:]
		s.dataMemSize = s.pkt.MemSize()
		s.dataDropped = true
		oldPkt.DecRef()
	}

	s.IncRef()
	q.list.PushBack(s)
	s.setOwner(q.ep, recvQ)
	return true
}

// dequeue removes and returns the next segment from queue, if one exists.
// Ownership is transferred to the caller, who is responsible for decrementing
// the ref count when done.
//
// +checklocksexclude:q.mu
func (q *segmentQueue) dequeue() *segment {
	q.mu.Lock()
	defer q.mu.Unlock()

	s := q.list.Front()
	if s != nil {
		q.list.Remove(s)
	}

	return s
}

// freeze prevents any more segments from being added to the queue. i.e all
// future segmentQueue.enqueue will return false and not add the segment to the
// queue till the queue is unfroze with a corresponding segmentQueue.thaw call.
//
// +checklocksexclude:q.mu
func (q *segmentQueue) freeze() {
	q.mu.Lock()
	defer q.mu.Unlock()
	q.frozen = true
}

// thaw unfreezes a previously frozen queue using segmentQueue.freeze() and
// allows new segments to be queued again.
//
// +checklocksexclude:q.mu
func (q *segmentQueue) thaw() {
	q.mu.Lock()
	defer q.mu.Unlock()
	q.frozen = false
}
