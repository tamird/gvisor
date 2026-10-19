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
	"sync/atomic"
	"time"

	"gvisor.dev/gvisor/pkg/sentry/vfs"
	"gvisor.dev/gvisor/pkg/sync"
)

// These cumulative bounds deliberately do not reclaim enrollment from a racy
// zero-reader/zero-writer sample. Exhaustion makes later observations incomplete.
// This fork-only diagnostic is not an inventory of all reference owners.
const (
	maxWriterPipes = 256
	maxPipeWriters = 8
	maxPipeWrites  = 8
)

const (
	gapWriterBusy = 1 << iota
	gapWriterLimit
	gapWriteBusy
	gapWriteLimit
	gapSampleBusy
)

type releasePhase uint32

const (
	releaseLive releasePhase = iota
	releaseFinalCleanup
	releasePipeEntered
	releaseWriterClosed
	releaseNotified
	releaseComplete
)

func (p releasePhase) String() string {
	switch p {
	case releaseLive:
		return "LIVE"
	case releaseFinalCleanup:
		return "FINAL_CLEANUP"
	case releasePipeEntered:
		return "PIPE_RELEASE"
	case releaseWriterClosed:
		return "WRITER_CLOSED"
	case releaseNotified:
		return "NOTIFY_RETURNED"
	case releaseComplete:
		return "DESTRUCTOR_DONE"
	default:
		return "UNKNOWN"
	}
}

type writePhase uint32

const (
	writeDone writePhase = iota
	writeHeld
	writeWaiting
	writeDecRefStarted
)

const writePhaseMask = 3

func (p writePhase) String() string {
	switch p {
	case writeDone:
		return "DONE"
	case writeHeld:
		return "HELD"
	case writeWaiting:
		return "REGISTERED_WAIT"
	case writeDecRefStarted:
		return "DECREF_STARTED"
	default:
		return "UNKNOWN"
	}
}

// All registry fields are reached only through nosave pointers. Go pointers
// keep atomic counters readable; no diagnostic takes a manual file reference.
// Released descriptions retain tombstones until this Pipe becomes unreachable.
type writerRegistry struct {
	mu         sync.Mutex
	config     *ReadDiagnostics
	id         uint64
	creator    ReadIdentity
	incomplete atomic.Uint64
	writers    [maxPipeWriters]*writerRecord
	writes     [maxPipeWrites]writeRecord
	nextWrite  uint64
}

type writerRecord struct {
	id         uint64
	refs       *vfs.FileDescriptionRefs
	opened     atomic.Bool
	phase      atomic.Uint32
	milestones atomic.Uint32
}

type writeRecord struct {
	writerID  uint64
	identity  ReadIdentity
	operation string
	start     time.Time
	// Upper bits identify the operation; low bits contain writePhase. A stale
	// observation cannot update a reused slot. Identity is protected by mu.
	state atomic.Uint64
}

// EnrollWriter observes a freshly-created anonymous pipe before either end is
// published in an FD table. writer must be its initial connected write end.
// Disabled diagnostics allocate nothing. No task-name assumption is made about
// the creator: the existing late reader filter selects observations instead.
func (fd *VFSPipeFD) EnrollWriter(d *ReadDiagnostics, writer *VFSPipeFD, creator ReadIdentity) {
	if d == nil {
		return
	}
	for {
		n := d.writerPipes.Load()
		if n == maxWriterPipes {
			d.writerPipeSkips.Add(1)
			return
		}
		if d.writerPipes.CompareAndSwap(n, n+1) {
			fd.pipe.writerDiagnostics = &writerRegistry{config: d, id: n + 1, creator: creator}
			break
		}
	}
	fd.pipe.writerDiagnostics.register(writer)
	writer.observeWriterOpened()
}

func (r *writerRegistry) register(fd *VFSPipeFD) {
	if !r.mu.TryLock() {
		r.incomplete.Or(gapWriterBusy)
		return
	}
	defer r.mu.Unlock()
	for i, record := range r.writers {
		if record == nil {
			record = &writerRecord{id: r.config.writerIDs.Add(1), refs: &fd.vfsfd.FileDescriptionRefs}
			r.writers[i] = record
			fd.writerDiagnostic = record
			return
		}
	}
	r.incomplete.Or(gapWriterLimit)
}

func (fd *VFSPipeFD) observeWriterOpened() {
	if fd.writerDiagnostic != nil {
		fd.writerDiagnostic.opened.Store(true)
	}
}

func (fd *VFSPipeFD) observeRelease(phase releasePhase) {
	if fd.writerDiagnostic != nil {
		fd.writerDiagnostic.phase.Store(uint32(phase))
		fd.writerDiagnostic.milestones.Or(1 << phase)
	}
}

// ObserveFinalReleaseStart is the optional fork-only VFS final-zero hook.
// No lock, allocation, logging, Task access or reference operation occurs here.
func (fd *VFSPipeFD) ObserveFinalReleaseStart() {
	fd.observeRelease(releaseFinalCleanup)
}

// ObserveFinalReleaseDone records completion of the actual VFS destructor.
func (fd *VFSPipeFD) ObserveFinalReleaseDone() {
	fd.observeRelease(releaseComplete)
}

// WriteObservation names a write/writev's existing syscall reference. It does
// not retain that reference. Generation-checked atomic updates prevent a stale
// observation from changing a reused slot. No write allocates a diagnostic record.
type WriteObservation struct {
	record     *writeRecord
	generation uint64
}

// HasWriterDiagnostic avoids collecting task identity for unenrolled traffic.
// The registration is immutable after this file description is published.
func (fd *VFSPipeFD) HasWriterDiagnostic() bool {
	return fd.writerDiagnostic != nil
}

// ObserveWrite must be called after the syscall's actual GetFile. Other private
// holders are deliberately unclassified; missing records never imply absence.
func (fd *VFSPipeFD) ObserveWrite(id ReadIdentity, operation string) WriteObservation {
	r := fd.pipe.writerDiagnostics
	if r == nil || fd.writerDiagnostic == nil {
		return WriteObservation{}
	}
	if !r.mu.TryLock() {
		r.incomplete.Or(gapWriteBusy)
		return WriteObservation{}
	}
	defer r.mu.Unlock()
	for i := range r.writes {
		record := &r.writes[i]
		if writePhase(record.state.Load()&writePhaseMask) == writeDone {
			if r.nextWrite == ^uint64(0)>>2 {
				r.incomplete.Or(gapWriteLimit)
				return WriteObservation{}
			}
			r.nextWrite++
			record.writerID, record.identity, record.operation, record.start = fd.writerDiagnostic.id, id, operation, time.Now()
			record.state.Store(r.nextWrite<<2 | uint64(writeHeld))
			return WriteObservation{record: record, generation: r.nextWrite}
		}
	}
	r.incomplete.Or(gapWriteLimit)
	return WriteObservation{}
}

// Waiting marks only the registered retry's actual BlockWithDeadline interval.
func (o WriteObservation) Waiting() {
	o.setPhase(writeWaiting)
}

// Resumed marks return from BlockWithDeadline, before retry or error handling.
func (o WriteObservation) Resumed() {
	o.setPhase(writeHeld)
}

// DecRefStarted ends the claim of a held reference. The subsequent DecRef may
// already have decremented it while blocked in final cleanup.
func (o WriteObservation) DecRefStarted() {
	o.setPhase(writeDecRefStarted)
}

// Done must follow the actual DecRef return. This atomic transition alone makes
// the fixed slot reclaimable; no later removal can clear a newer operation.
func (o WriteObservation) Done() {
	o.setPhase(writeDone)
}

func (o WriteObservation) setPhase(phase writePhase) {
	if o.record == nil {
		return
	}
	for {
		state := o.record.state.Load()
		if state>>2 != o.generation || writePhase(state&writePhaseMask) == writeDone {
			return
		}
		if o.record.state.CompareAndSwap(state, o.generation<<2|uint64(phase)) {
			return
		}
	}
}

type writerSample struct {
	ID                      uint64
	Opened                  bool
	RefsBefore, RefsAfter   int64
	PhaseBefore, PhaseAfter releasePhase
	Milestones              uint32
}

type writeSample struct {
	ID, WriterID            uint64
	Identity                ReadIdentity
	Operation               string
	Age                     time.Duration
	PhaseBefore, PhaseAfter writePhase
}

type writerSnapshot struct {
	Enrolled   bool
	PipeID     uint64
	Creator    ReadIdentity
	Incomplete uint64
	Busy       bool
	Writers    []writerSample
	Writes     []writeSample
}

func (p *Pipe) sampleWriters(now time.Time) writerSnapshot {
	r := p.writerDiagnostics
	if r == nil {
		return writerSnapshot{} // Unenrolled, not proof of no writers.
	}
	s := writerSnapshot{Enrolled: true, PipeID: r.id, Creator: r.creator}
	if !r.mu.TryLock() {
		r.incomplete.Or(gapSampleBusy)
		s.Busy, s.Incomplete = true, r.incomplete.Load()
		return s
	}
	defer r.mu.Unlock()
	for _, record := range r.writers {
		if record != nil {
			w := writerSample{ID: record.id, Opened: record.opened.Load(), RefsBefore: record.refs.ReadRefs(), PhaseBefore: releasePhase(record.phase.Load())}
			w.RefsAfter, w.PhaseAfter = record.refs.ReadRefs(), releasePhase(record.phase.Load())
			w.Milestones = record.milestones.Load()
			s.Writers = append(s.Writers, w)
		}
	}
	for i := range r.writes {
		record := &r.writes[i]
		state := record.state.Load()
		if writePhase(state&writePhaseMask) != writeDone {
			w := writeSample{ID: state >> 2, WriterID: record.writerID, Identity: record.identity, Operation: record.operation, Age: now.Sub(record.start), PhaseBefore: writePhase(state & writePhaseMask)}
			w.PhaseAfter = writePhase(record.state.Load() & writePhaseMask)
			s.Writes = append(s.Writes, w)
		}
	}
	s.Incomplete = r.incomplete.Load()
	return s
}
