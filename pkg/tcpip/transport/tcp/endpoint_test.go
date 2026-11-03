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

// TestProcessorFIFOBoundary exercises finite work on listeners with no NICs.
// Each release records actual consumption by the listening handler. The old
// policy gets an explicit scheduler escape at two batches or six lock misses;
// neither run can depend on a timer or asynchronous preemption to end.
func TestProcessorFIFOBoundary(t *testing.T) {
	old := runtime.GOMAXPROCS(1)
	defer runtime.GOMAXPROCS(old)
	st := stack.New(stack.Options{})
	defer st.Destroy()
	t.Logf("FIFO_ENV go=%s os=%s arch=%s p=%d debug=%s", runtime.Version(), runtime.GOOS, runtime.GOARCH, runtime.GOMAXPROCS(0), os.Getenv("GODEBUG"))
	const limit = 3*maxSegmentsPerWake + 1
	for _, scenario := range []string{"refill", "foreign", "same", "stale", "alone"} {
		var d, parked dispatcher
		d.processors = make([]processor, 1)
		parked.processors = make([]processor, 1)
		p, q := &d.processors[0], &parked.processors[0]
		newEP := func() *Endpoint {
			e := &Endpoint{stack: st}
			e.segmentQueue.ep = e
			e.state.Store(uint32(StateListen))
			return e
		}
		a, b, peer, late, stale := newEP(), newEP(), newEP(), newEP(), newEP()
		var ca, cb, cp, cl atomic.Int32
		add := func(e *Endpoint, release func()) {
			s := newSegment()
			s.pkt = stack.NewPacketBuffer(stack.PacketBufferOptions{OnRelease: release})
			s.flags = header.TCPFlagRst
			if !e.enqueueSegment(s) {
				panic("unable to queue local work")
			}
			s.DecRef()
		}
		var refill func()
		refill = func() {
			if cp.Add(1) < limit {
				add(peer, refill)
			}
		}
		add(a, func() { ca.Add(1) })
		a.StopWork()
		p.epQ.enqueue(a)
		if scenario == "refill" || scenario == "same" {
			add(peer, refill)
			p.epQ.enqueue(peer)
		}
		if scenario == "foreign" {
			add(b, func() { cb.Add(1) })
			b.StopWork()
			p.epQ.enqueue(b)
		}
		if scenario == "stale" {
			p.epQ.enqueue(stale)
		}
		if scenario != "foreign" {
			add(late, func() { cl.Add(1) })
		}
		runnable := make(chan struct{})
		var released, escaped atomic.Bool
		var attempts, productive, yields atomic.Int32
		var otherClaim, localDedup, joined, sameClaim atomic.Bool
		var staleVisits atomic.Int32
		p.researchVisit = func(ep *Endpoint) {
			if ep == stale {
				staleVisits.Add(1)
			}
		}
		p.researchBefore = func(ep *Endpoint) {
			if ep != a || attempts.Load() != 0 {
				return
			}
			switch scenario {
			case "foreign":
				q.queueEndpoint(a)
			case "same":
				p.queueEndpoint(a)
			}
		}
		p.researchAfter = func(ep *Endpoint) {
			if ep == a || ep == b {
				n := attempts.Add(1)
				if n == 1 {
					if scenario == "foreign" {
						q.epQ.mu.Lock()
						otherClaim.Store(q.epQ.list.Front() == a && q.epQ.list.Back() == a)
						q.epQ.mu.Unlock()
						p.epQ.mu.Lock()
						localDedup.Store(p.epQ.list.Front() == b && p.epQ.list.Back() == b)
						p.epQ.mu.Unlock()
					}
					if scenario == "same" {
						p.epQ.mu.Lock()
						sameClaim.Store(p.epQ.list.Front() == peer && p.epQ.list.Back() == a)
						p.epQ.mu.Unlock()
					}
					if scenario != "foreign" {
						p.queueEndpoint(late)
						joined.Store(true)
					}
					close(runnable)
				}
			}
			if ep == peer {
				productive.Add(1)
			}
			if !released.Load() && (attempts.Load() >= 6 || productive.Load() >= 2) {
				escaped.Store(true)
				runtime.Gosched()
			}
		}
		var cutReady, cutLate, cutMiss, cutStale atomic.Int32
		var cutHeld atomic.Bool
		p.researchYield = func() {
			if yields.Add(1) == 1 {
				cutReady.Store(cp.Load())
				cutLate.Store(cl.Load())
				cutMiss.Store(attempts.Load())
				cutStale.Store(staleVisits.Load())
				cutHeld.Store(!released.Load())
			}
		}
		d.start()
		p.newEndpointWaker.Assert()
		<-runnable
		atResume, lateResume := cp.Load(), cl.Load()
		atStale := staleVisits.Load()
		atMiss, atBatches, atYield := attempts.Load(), productive.Load(), yields.Load()
		released.Store(true)
		a.ResumeWork()
		if scenario == "foreign" {
			b.ResumeWork()
			parked.start()
		}
		<-p.pause()
		empty := p.epQ.empty()
		p.resume()
		p.close()
		d.wait()
		if scenario == "foreign" {
			<-q.pause()
			empty = empty && q.epQ.empty()
			q.resume()
			q.close()
			parked.wait()
		}
		t.Logf("FIFO_ROW kind=%s batch=%d resumed=%d late_resume=%d misses=%d batches=%d yields=%d escape=%t other=%t dedup=%t same=%t stale=%d joined=%t a=%d b=%d peer=%d late=%d empty=%t cut_ready=%d cut_late=%d cut_miss=%d cut_stale=%d cut_held=%t", scenario, maxSegmentsPerWake, atResume, lateResume, atMiss, atBatches, atYield, escaped.Load(), otherClaim.Load(), localDedup.Load(), sameClaim.Load(), atStale, joined.Load(), ca.Load(), cb.Load(), cp.Load(), cl.Load(), empty, cutReady.Load(), cutLate.Load(), cutMiss.Load(), cutStale.Load(), cutHeld.Load())
	}
}
