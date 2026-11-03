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
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/sync"
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

// TestSinglePRunnableOwner isolates the scheduling decision after TryLock
// fails. A second endpoint is ready on the same FIFO; neither sends packets.
func TestSinglePRunnableOwner(t *testing.T) {
	old := runtime.GOMAXPROCS(1)
	defer runtime.GOMAXPROCS(old)
	t.Logf("OWNER_ENV go=%s os=%s arch=%s procs=%d godebug=%s", runtime.Version(), runtime.GOOS, runtime.GOARCH, runtime.GOMAXPROCS(0), os.Getenv("GODEBUG"))
	for pair := 0; pair < 12; pair++ {
		for i := 0; i < 2; i++ {
			yield := (i+pair)%2 == 1
			var held, peer Endpoint
			var q epQueue
			held.StopWork()
			q.enqueue(&held)
			q.enqueue(&peer)

			// Closing runnable makes the holder eligible to resume without
			// blocking the worker. The release supplies no wakeup to the FIFO.
			runnable := make(chan struct{})
			peerDone := make(chan struct{})
			type result struct {
				retries, yields  int
				acquired, capped bool
			}
			finished := make(chan result, 1)
			var start time.Time // Published to the holder by closing runnable.
			go func() {
				var r result
				for {
					e := q.dequeue()
					if e == nil {
						break
					}
					if e.TryLock() {
						e.ResumeWork()
						if e == &peer {
							close(peerDone)
						} else {
							r.acquired = true
						}
						continue
					}
					r.retries++
					if r.retries == 1 {
						start = time.Now()
						close(runnable)
					}
					q.enqueue(e)
					if r.retries >= 1<<20 || (r.retries%256 == 0 && time.Since(start) >= 80*time.Millisecond) {
						r.capped = true
						break
					}
					if yield {
						r.yields++
						runtime.Gosched()
					}
				}
				finished <- r
			}()
			<-runnable
			delay := time.Since(start)
			peerServed := func() bool {
				select {
				case <-peerDone:
					return true
				default:
					return false
				}
			}
			atResume := peerServed()
			held.ResumeWork()
			atRelease := peerServed()
			r := <-finished
			t.Logf("OWNER_SAMPLE pair=%d yield=%t ns=%d retries=%d yields=%d peer_at_resume=%t peer_at_release=%t peer_served=%t held_served=%t capped=%t", pair, yield, delay.Nanoseconds(), r.retries, r.yields, atResume, atRelease, peerServed(), r.acquired, r.capped)
		}
	}
}
