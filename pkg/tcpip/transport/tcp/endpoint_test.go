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
	"os"
	"runtime"
	"sync/atomic"
	"testing"
	"time"

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

// TestProcessorContendedPass puts bounded, local work on real listen endpoints.
// They have no network interfaces. A discarded item calls OnRelease when the
// listening handler actually consumes it; observing the FIFO alone is not enough.
func TestProcessorContendedPass(t *testing.T) {
	old := runtime.GOMAXPROCS(1)
	defer runtime.GOMAXPROCS(old)
	st := stack.New(stack.Options{})
	defer st.Destroy()
	t.Logf("PASS_ENV go=%s os=%s arch=%s p=%d debug=%s", runtime.Version(), runtime.GOOS, runtime.GOARCH, runtime.GOMAXPROCS(0), os.Getenv("GODEBUG"))

	for i := 0; i < 12; i++ {
		for _, scenario := range []string{"backlog", "arrival"} {
			var d dispatcher
			d.processors = make([]processor, 1)
			p := &d.processors[0]
			var count [4]atomic.Int32
			makeEP := func(idx, n int) *Endpoint {
				e := &Endpoint{stack: st}
				e.segmentQueue.ep = e
				e.state.Store(uint32(StateListen))
				for j := 0; j < n; j++ {
					s := newSegment()
					s.pkt = stack.NewPacketBuffer(stack.PacketBufferOptions{OnRelease: func() { count[idx].Add(1) }})
					s.flags = header.TCPFlagRst // The listener discards without sending a reply.
					if !e.enqueueSegment(s) {
						t.Fatal("unable to queue local work")
					}
					s.DecRef()
				}
				return e
			}

			a, b := makeEP(0, 1), makeEP(1, 1)
			a.StopWork()
			b.StopWork()
			p.epQ.enqueue(a)
			p.epQ.enqueue(b)
			var peer, late *Endpoint
			var backlog int32
			if scenario == "backlog" {
				backlog = 2*maxSegmentsPerWake + 1
				peer = makeEP(2, int(backlog))
				p.epQ.enqueue(peer)
			} else {
				late = makeEP(3, 1)
			}

			// One private observation point, after the original requeue. Each
			// held endpoint has one item, so it only requeues when still busy.
			runnable := make(chan struct{})
			var start time.Time
			var misses, otherMisses, batches int
			var insertedBehind, capped bool
			p.researchRequeue = func(ep *Endpoint) {
				switch ep {
				case a:
					misses++
					if (scenario == "backlog" && misses == 1) || (scenario == "arrival" && misses == 2) {
						if late != nil {
							p.epQ.mu.Lock()
							insertedBehind = p.epQ.list.Back() == a
							p.epQ.mu.Unlock()
							p.queueEndpoint(late)
						}
						start = time.Now()
						close(runnable)
					}
					if misses >= 1<<20 || (misses%256 == 0 && !start.IsZero() && time.Since(start) >= 80*time.Millisecond) {
						capped = true
						runtime.Gosched()
					}
				case b:
					otherMisses++
				case peer:
					batches++
				}
			}
			d.start()
			p.newEndpointWaker.Assert()
			<-runnable
			delay := time.Since(start)
			atResume, lateAtResume := count[2].Load(), count[3].Load()
			a.ResumeWork()
			b.ResumeWork()
			atRelease, lateAtRelease := count[2].Load(), count[3].Load()

			// The original pause handshake waits for a drained FIFO.
			<-p.pause()
			empty := p.epQ.empty()
			p.resume()
			p.close()
			d.wait()
			t.Logf("PASS_ROW i=%d kind=%s ns=%d miss=%d other=%d backlog=%d batches=%d peer_resume=%d peer_release=%d late_resume=%d late_release=%d a=%d b=%d peer=%d late=%d behind=%t empty=%t capped=%t", i, scenario, delay.Nanoseconds(), misses, otherMisses, backlog, batches, atResume, atRelease, lateAtResume, lateAtRelease, count[0].Load(), count[1].Load(), count[2].Load(), count[3].Load(), insertedBehind, empty, capped)
		}
	}
}
