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
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/abi/linux"
	"gvisor.dev/gvisor/pkg/context"
	"gvisor.dev/gvisor/pkg/sentry/vfs"
)

func TestWriterDiagnosticOperationSlots(t *testing.T) {
	runTest(t, DefaultPipeSize, func(ctx context.Context, r, w *vfs.FileDescription) {
		reader, writer := r.Impl().(*VFSPipeFD), w.Impl().(*VFSPipeFD)
		reader.EnrollWriter(nil, writer, ReadIdentity{})
		if reader.pipe.writerDiagnostics != nil {
			t.Fatal("disabled diagnostic allocated a registry")
		}
		d := NewReadDiagnostics(time.Hour, "copier")
		reader.EnrollWriter(d, writer, ReadIdentity{TID: 1})
		var operations []WriteObservation
		defer func() {
			for _, operation := range operations {
				operation.DecRefStarted()
				w.DecRef(ctx)
				operation.Done()
			}
		}()
		for i := 0; i < maxPipeWrites; i++ {
			w.IncRef() // The actual reference owned by this operation.
			operation := writer.ObserveWrite(ReadIdentity{TID: int32(i + 10), FD: 2}, "writev")
			operations = append(operations, operation)
			if operation.record == nil {
				t.Fatalf("operation %d rejected within capacity", i)
			}
		}
		if got := writer.ObserveWrite(ReadIdentity{}, "write"); got.record != nil {
			t.Fatal("active operation capacity exceeded")
		}
		before := reader.pipe.sampleWriters(time.Now())
		if len(before.Writes) != maxPipeWrites || len(before.Writers) != 1 || before.Writers[0].RefsAfter != maxPipeWrites+1 || before.Incomplete&gapWriteLimit == 0 {
			t.Fatalf("reference ownership or overflow evidence: %+v", before)
		}
		old := operations[0]
		old.DecRefStarted()
		w.DecRef(ctx)
		old.Done()
		w.IncRef()
		next := writer.ObserveWrite(ReadIdentity{TID: 99, FD: 3}, "write")
		operations[0] = next
		if next.record != old.record || next.generation <= old.generation {
			t.Fatal("completed fixed slot was not reused with a fresh generation")
		}
		old.Waiting()
		old.DecRefStarted()
		old.Done()
		state := next.record.state.Load()
		if state>>2 != next.generation || writePhase(state&writePhaseMask) != writeHeld {
			t.Fatalf("stale operation changed a reused slot: %d", state)
		}
		next.Waiting()
		during := reader.pipe.sampleWriters(time.Now())
		if len(during.Writes) != maxPipeWrites || during.Writes[0].Identity.TID != 99 || during.Writes[0].PhaseAfter != writeWaiting || during.Incomplete&gapWriteLimit == 0 {
			t.Fatalf("new wait identity or sticky gap lost: %+v", during)
		}
		next.Resumed()
	})
}

func TestWriterDiagnosticRegistrationGapsStayVisible(t *testing.T) {
	runTest(t, DefaultPipeSize, func(ctx context.Context, outer, _ *vfs.FileDescription) {
		vp := NewVFSPipe(false, DefaultPipeSize)
		r, w, err := vp.ReaderWriterPair(ctx, outer.Mount(), outer.Dentry(), 0)
		if err != nil {
			t.Fatal(err)
		}
		defer r.DecRef(ctx)
		defer w.DecRef(ctx)
		reader, writer := r.Impl().(*VFSPipeFD), w.Impl().(*VFSPipeFD)
		reader.EnrollWriter(NewReadDiagnostics(time.Hour, "copier"), writer, ReadIdentity{})
		registry := reader.pipe.writerDiagnostics
		registry.mu.Lock()
		busy := reader.pipe.sampleWriters(time.Now())
		operation := writer.ObserveWrite(ReadIdentity{}, "write")
		unrecorded, err := vp.Open(ctx, outer.Mount(), outer.Dentry(), linux.O_WRONLY, &vfs.FileLocks{})
		registry.mu.Unlock()
		if err != nil {
			t.Fatal(err)
		}
		unrecorded.DecRef(ctx)
		if !busy.Busy || operation.record != nil {
			t.Fatal("busy registry was treated as complete")
		}
		// Closed records deliberately consume capacity, preserving their identity
		// instead of letting a late sample mistake release for never registered.
		for i := 1; i <= maxPipeWriters; i++ {
			reopened, err := vp.Open(ctx, outer.Mount(), outer.Dentry(), linux.O_WRONLY, &vfs.FileLocks{})
			if err != nil {
				t.Fatal(err)
			}
			reopened.DecRef(ctx)
		}
		after := reader.pipe.sampleWriters(time.Now())
		const wantGaps = gapSampleBusy | gapWriteBusy | gapWriterBusy | gapWriterLimit
		if after.Busy || after.Incomplete != wantGaps || len(after.Writers) != maxPipeWriters || len(after.Writes) != 0 || reader.pipe.writers.Load() != 1 {
			t.Fatalf("successful sample cleared gaps or changed writer lifetime: %+v", after)
		}
	})
}

func TestWriterDiagnosticEnrollmentLimit(t *testing.T) {
	runTest(t, DefaultPipeSize, func(ctx context.Context, outer, _ *vfs.FileDescription) {
		d := NewReadDiagnostics(time.Hour, "copier")
		for i := 0; i <= maxWriterPipes; i++ {
			vp := NewVFSPipe(false, DefaultPipeSize)
			r, w, err := vp.ReaderWriterPair(ctx, outer.Mount(), outer.Dentry(), 0)
			if err != nil {
				t.Fatal(err)
			}
			r.Impl().(*VFSPipeFD).EnrollWriter(d, w.Impl().(*VFSPipeFD), ReadIdentity{})
			sample := vp.Pipe().sampleWriters(time.Now())
			r.DecRef(ctx)
			w.DecRef(ctx)
			if sample.Enrolled != (i < maxWriterPipes) {
				t.Fatalf("pipe %d enrollment=%t", i, sample.Enrolled)
			}
			if !sample.Enrolled && len(sample.Writers) != 0 {
				t.Fatal("unenrolled pipe claimed writer records")
			}
		}
		if d.writerPipes.Load() != maxWriterPipes || d.writerPipeSkips.Load() != 1 {
			t.Fatalf("cumulative enrollment was silently recycled: pipes=%d skips=%d", d.writerPipes.Load(), d.writerPipeSkips.Load())
		}
	})
}
