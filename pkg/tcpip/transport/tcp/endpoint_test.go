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

	"gvisor.dev/gvisor/pkg/sync"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
)

func TestSendBufferStateSnapshot(t *testing.T) {
	var ep Endpoint
	var snapshot TCPSndBufState
	// Neither the initial nor updated endpoint state contains this value.
	snapshot.AutoTuneSndBufDisabled.Store(2)

	// The socket option callback does not take sndQueueMu. Join only after
	// both accesses so that the wait group cannot serialize them.
	var wg sync.WaitGroup
	wg.Go(func() {
		_ = ep.OnSetSendBufferSize(4096)
	})
	wg.Go(func() {
		ep.sndQueueInfo.sndQueueMu.Lock()
		ep.sndQueueInfo.CloneState(&snapshot)
		ep.sndQueueInfo.sndQueueMu.Unlock()
	})
	wg.Wait()

	ep.sndQueueInfo.sndQueueMu.Lock()
	sourceDisabled := ep.sndQueueInfo.AutoTuneSndBufDisabled.Load()
	ep.sndQueueInfo.sndQueueMu.Unlock()
	if sourceDisabled != 1 {
		t.Errorf("source AutoTuneSndBufDisabled = %d, want 1", sourceDisabled)
	}
	if got := snapshot.AutoTuneSndBufDisabled.Load(); got > 1 {
		t.Errorf("snapshot AutoTuneSndBufDisabled = %d, want 0 or 1", got)
	}
}

func TestProcessorYieldsAfterReadyEndpoints(t *testing.T) {
	st := stack.New(stack.Options{})
	defer st.Destroy()
	var p processor
	queueListener := func(packets int, consumed func()) *Endpoint {
		ep := &Endpoint{stack: st}
		ep.segmentQueue.ep = ep
		ep.state.Store(uint32(StateListen))
		for range packets {
			s := newSegment()
			s.flags = header.TCPFlagRst
			s.pkt = stack.NewPacketBuffer(stack.PacketBufferOptions{OnRelease: consumed})
			queued := ep.enqueueSegment(s)
			s.DecRef()
			if !queued {
				t.Fatal("listener rejected an empty RST segment")
			}
		}
		p.epQ.enqueue(ep)
		return ep
	}
	var blockedPackets, readyPackets int
	blocked := queueListener(1, func() { blockedPackets++ })
	queueListener(maxSegmentsPerWake+1, func() { readyPackets++ })

	func() {
		blocked.StopWork()
		defer blocked.ResumeWork()
		if !p.drainQueue() {
			t.Error("processor drained the queue while the first listener was locked")
		}
		if blockedPackets != 0 || readyPackets != maxSegmentsPerWake {
			t.Errorf("before yielding: processed %d blocked and %d ready packets, want 0 and %d", blockedPackets, readyPackets, maxSegmentsPerWake)
		}
	}()

	if p.drainQueue() || blockedPackets != 1 || readyPackets != maxSegmentsPerWake+1 {
		t.Errorf("after unlocking: processed %d blocked and %d ready packets, want 1 and %d with the queue drained", blockedPackets, readyPackets, maxSegmentsPerWake+1)
	}
}
