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

package main

import (
	"flag"
	"slices"
	"sync"
	"time"

	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
)

var burstMode = flag.String("burst_mode", "", "fork: open or hold the first bulk flight except its terminal MSS")

type linkDecision struct {
	At   int64
	Seq  uint32
	Size int
	Drop bool
}

var burst struct {
	sync.Mutex
	first        time.Time
	base         uint32
	port         uint16
	mss          int
	released     bool
	calls, drops int
	decisions    []linkDecision
}

func linkSnapshot() map[string]any {
	burst.Lock()
	defer burst.Unlock()
	return map[string]any{"mode": *burstMode, "base": burst.base, "port": burst.port, "mss": burst.mss,
		"released": burst.released, "drops": burst.drops, "decisions": slices.Clone(burst.decisions), "calls": burst.calls}
}

type lossQueue struct{ stack.QueueingDiscipline }

// The queue borrows p. Dropping here accepts the send without retaining p;
// other packets go through the ordinary FIFO, including the entire suffix.
func (q *lossQueue) WritePacket(p *stack.PacketBuffer) tcpip.Error {
	if p.TransportProtocolNumber == header.TCPProtocolNumber && p.Data().Size() > 0 {
		h := header.TCP(p.TransportHeader().Slice())
		if len(h) >= header.TCPMinimumSize && h.SourcePort() == 44000 && discard(h, p.Data().Size()) {
			return nil
		}
	}
	return q.QueueingDiscipline.WritePacket(p)
}

func discard(h header.TCP, size int) bool {
	burst.Lock()
	defer burst.Unlock()
	now := time.Now()
	if burst.first.IsZero() {
		if size != 1424 {
			return false
		}
		burst.first, burst.base, burst.port, burst.mss = now, h.SequenceNumber(), h.DestinationPort(), size
	}
	if h.DestinationPort() != burst.port {
		return false
	}
	burst.calls++
	if *burstMode == "held" && !burst.released && h.SequenceNumber() == burst.base && now.Sub(burst.first) >= 100*time.Millisecond {
		burst.released = true
	}
	drop := *burstMode == "held" && !burst.released && !(h.SequenceNumber() == burst.base+uint32(9*burst.mss) && size == burst.mss && burst.calls == 10)
	if drop {
		burst.drops++
	}
	if len(burst.decisions) < 128 || drop {
		if len(burst.decisions) < 512 {
			burst.decisions = append(burst.decisions, linkDecision{now.UnixNano(), h.SequenceNumber(), size, drop})
		}
	}
	return drop
}
