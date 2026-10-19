// Copyright 2026 The gVisor Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package pipe

import (
	"strings"
	"sync/atomic"
	"time"

	"gvisor.dev/gvisor/pkg/log"
	"gvisor.dev/gvisor/pkg/sync"
)

// This fork-only diagnostic observes the pipe used by an actual blocked read.
// It deliberately does not change deadlines, consume notifications, or retain
// FileDescription references. Delete it after the Java stall is understood.
const (
	maxReadObservers = 64
	maxReadReports   = 64
)

// ReadDiagnostics bounds observer/timer objects cached on live file descriptions,
// including idle objects. Released files return that capacity before an already
// running callback finishes; residual callbacks are separately bounded by the
// report limit. Exhaustion makes the diagnostic incomplete.
// Configuration is immutable after construction; it is not checkpointed.
type ReadDiagnostics struct {
	delay           time.Duration
	taskPrefix      string
	live            atomic.Int32
	created         atomic.Uint64
	skipped         atomic.Uint64
	reports         atomic.Uint64
	writerPipes     atomic.Uint64
	writerPipeSkips atomic.Uint64
	writerIDs       atomic.Uint64
}

// NewReadDiagnostics enables the temporary observer. The caller must validate
// a positive delay and nonempty task prefix before enabling it.
func NewReadDiagnostics(delay time.Duration, taskPrefix string) *ReadDiagnostics {
	if delay <= 0 || taskPrefix == "" {
		panic("pipe read diagnostics require a positive delay and nonempty task prefix")
	}
	d := &ReadDiagnostics{delay: delay, taskPrefix: taskPrefix}
	log.Warningf("PIPE-READ-DIAGNOSTIC enabled delay=%s task_prefix=%q live_timer_limit=%d cumulative_pipe_limit=%d writer_limit=%d active_write_limit=%d; sampled state is not atomic", delay, taskPrefix, maxReadObservers, maxWriterPipes, maxPipeWriters, maxPipeWrites)
	return d
}

// LogSummary reports selection gaps even when no timer fires. An unmatched
// prefix, exhaustion, missing summary, or no late observation is inconclusive.
func (d *ReadDiagnostics) LogSummary() {
	log.Warningf("PIPE-READ-DIAGNOSTIC summary created=%d live=%d skipped=%d reports=%d enrolled_pipes=%d skipped_pipes=%d writer_descriptions=%d", d.created.Load(), d.live.Load(), d.skipped.Load(), d.reports.Load(), d.writerPipes.Load(), d.writerPipeSkips.Load(), d.writerIDs.Load())
}

// ReadIdentity is captured on the syscall task, before a timer is started.
// FD is the original syscall argument, not a later lookup that could be reused.
type ReadIdentity struct {
	TID  int32
	TGID int32
	FD   int32
	Name string
}

// readObserver belongs to a VFSPipeFD but does not point back to it. Its timer
// retains only the Go Pipe object, immutable IDs, and synchronized wait state.
// No callback invokes a Task method or affects a manual file reference count.
type readObserver struct {
	mu         sync.Mutex
	config     *ReadDiagnostics
	pipe       *Pipe
	timer      *time.Timer
	generation uint64
	active     *ReadObservation
	closed     bool
}

// ReadObservation identifies one uninterrupted wait. End invalidates it before
// the syscall retries or unregisters the waiter. The channel is never consumed.
type ReadObservation struct {
	observer   *readObserver
	identity   ReadIdentity
	generation uint64
	start      time.Time
	due        time.Time
	channel    <-chan struct{}
	reported   bool // protected by observer.mu
}

// ObserveRead selects only the requested task prefix on anonymous pipes. It
// caches at most one timer per file description, rather than allocating one for
// every ordinary pipe wait. Concurrent selected readers are skipped explicitly.
// The caller holds the normal syscall reference to fd until End returns.
func (fd *VFSPipeFD) ObserveRead(d *ReadDiagnostics, id ReadIdentity, channel <-chan struct{}) *ReadObservation {
	if d == nil || fd.pipe.isNamed || !strings.HasPrefix(id.Name, d.taskPrefix) {
		return nil
	}
	fd.readObserverMu.Lock()
	o := fd.readObserver
	if o == nil {
		if n := d.live.Add(1); n > maxReadObservers {
			d.live.Add(-1)
			d.skipped.Add(1)
			fd.readObserverMu.Unlock()
			return nil
		}
		o = &readObserver{config: d, pipe: fd.pipe}
		fd.readObserver = o
		d.created.Add(1)
	}
	fd.readObserverMu.Unlock()

	o.mu.Lock()
	defer o.mu.Unlock()
	if o.active != nil || o.closed {
		d.skipped.Add(1)
		return nil
	}
	o.generation++
	now := time.Now()
	w := &ReadObservation{observer: o, identity: id, generation: o.generation, start: now, due: now.Add(d.delay), channel: channel}
	o.active = w
	if o.timer == nil {
		o.timer = time.AfterFunc(d.delay, o.observe)
	} else {
		o.timer.Reset(d.delay)
	}
	return w
}

// End cancels this wait without joining a callback or waiting for log I/O.
// An already-running callback detects the changed generation. It can safely
// finish using its Go Pipe object after the FileDescription has been released.
func (w *ReadObservation) End() {
	if w == nil {
		return
	}
	o := w.observer
	o.mu.Lock()
	if o.active == w {
		o.active = nil
		o.timer.Stop()
	}
	o.mu.Unlock()
}

func (o *readObserver) close() {
	o.mu.Lock()
	o.closed = true
	o.active = nil
	if o.timer != nil {
		o.timer.Stop()
	}
	o.mu.Unlock()
	o.config.live.Add(-1)
}

// readSample is a timestamped observation, not a globally atomic snapshot:
// wClose updates writers without taking pipe.mu. TryLock avoids getting stuck
// behind the very pipe operation being diagnosed.
type readSample struct {
	LockAcquired                          bool
	Size                                  int64
	HadWriter                             bool
	ReadersBefore, ReadersAfter           int32
	WritersBefore, WritersAfter           int32
	TotalReadersBefore, TotalReadersAfter int32
	TotalWritersBefore, TotalWritersAfter int32
}

func (p *Pipe) sampleRead() readSample {
	s := readSample{ReadersBefore: p.readers.Load(), WritersBefore: p.writers.Load(), TotalReadersBefore: p.totalReaders.Load(), TotalWritersBefore: p.totalWriters.Load()}
	if p.mu.TryLock() {
		s.LockAcquired = true
		s.Size, s.HadWriter = p.size, p.hadWriter
		p.mu.Unlock()
	}
	s.ReadersAfter, s.WritersAfter = p.readers.Load(), p.writers.Load()
	s.TotalReadersAfter, s.TotalWritersAfter = p.totalReaders.Load(), p.totalWriters.Load()
	return s
}

type readReport struct {
	Identity                    ReadIdentity
	Generation                  uint64
	Wait                        time.Duration
	Unchanged                   bool
	PendingBefore, PendingAfter int
	State                       readSample
	Writers                     writerSnapshot
}

// sample claims and samples the actual wait. Passing the clock sample explicitly
// lets controls trigger this same observation without sleeps or global clocks.
func (o *readObserver) sample(now time.Time) (readReport, bool) {
	o.mu.Lock()
	w := o.active
	// Stop/Reset does not join an old callback. Check the current absolute due
	// time and claim the generation once, so it cannot observe a new wait early
	// or emit a second observation after the real callback has already fired.
	if w == nil || w.reported || now.Before(w.due) {
		o.mu.Unlock()
		return readReport{}, false
	}
	w.reported = true
	o.mu.Unlock()
	for {
		n := o.config.reports.Load()
		if n == maxReadReports {
			o.config.skipped.Add(1)
			return readReport{}, false
		}
		if o.config.reports.CompareAndSwap(n, n+1) {
			break
		}
	}

	r := readReport{Identity: w.identity, Generation: w.generation, Wait: now.Sub(w.start), PendingBefore: len(w.channel)}
	r.State = o.pipe.sampleRead()
	r.Writers = o.pipe.sampleWriters(now)
	r.PendingAfter = len(w.channel)
	o.mu.Lock()
	r.Unchanged = o.active == w && !o.closed
	o.mu.Unlock()
	return r, true
}

func (o *readObserver) observe() {
	now := time.Now()
	r, ok := o.sample(now)
	if !ok {
		return
	}
	// No locks, Task, or FileDescription references are held during output.
	// The separate runsc stack capture contains TID but not this generation.
	log.Warningf("PIPE-READ-DIAGNOSTIC sample time=%s pipe=%p phase=registered-retry-would-block observation=%+v; non-atomic state, raw refs include speculative bits, unnamed owners UNKNOWN, DECREF_STARTED is release progress, missing enrollment/gaps INCOMPLETE, later stacks separately timed", now.UTC().Format(time.RFC3339Nano), o.pipe, r)
}
