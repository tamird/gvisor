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

package state

import (
	"errors"
	"math"
	"runtime"
	"runtime/debug"
	"strconv"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/gccontrol"
	"gvisor.dev/gvisor/pkg/sentry/usage"
)

func TestSuspendGCForRestore(t *testing.T) {
	const (
		defaultGCPercent = 100
		queryMemoryLimit = -1
	)
	runtime.GC()
	prevP := debug.SetGCPercent(defaultGCPercent)
	prevL := debug.SetMemoryLimit(math.MaxInt64)
	prevMaxMem := usage.MaximumTotalMemoryBytes
	t.Cleanup(func() {
		usage.MaximumTotalMemoryBytes = prevMaxMem
		debug.SetGCPercent(prevP)
		debug.SetMemoryLimit(prevL)
	})

	const defaultLimit = int64(1 << 30)
	for _, tc := range []struct {
		name     string
		totalMem uint64
		metadata map[string]string
		want     int64
	}{
		{name: "unlimitedDefault", want: defaultLimit},
		{name: "malformedMetadataIgnored", totalMem: 200 << 20, metadata: map[string]string{metadataMemoryFileUsedBytesKey: "bad"}, want: 100 << 20},
		{
			name:     "subtractsSavedMemoryFileBytes",
			totalMem: 200 << 20,
			metadata: map[string]string{metadataMemoryFileUsedBytesKey: strconv.FormatUint(80<<20, 10)},
			want:     60 << 20,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			usage.MaximumTotalMemoryBytes = tc.totalMem
			s := SuspendGCForRestore(tc.metadata)
			defer s.End()
			if got := debug.SetMemoryLimit(queryMemoryLimit); got != tc.want {
				t.Fatalf("MemoryLimit = %d, want %d", got, tc.want)
			}
		})
	}
}

func TestFinishSaveGCSuspension(t *testing.T) {
	const (
		defaultGCPercent = 100
		queryMemoryLimit = -1
		resumeAfterSave  = true
		exitAfterSave    = false
	)
	setup := func(t *testing.T) *gccontrol.Suspension {
		t.Helper()
		runtime.GC()
		prevP := debug.SetGCPercent(defaultGCPercent)
		prevL := debug.SetMemoryLimit(math.MaxInt64)
		t.Cleanup(func() {
			debug.SetGCPercent(prevP)
			debug.SetMemoryLimit(prevL)
		})
		s := gccontrol.Suspend(gccontrol.NoMemoryLimit, 0)
		if s == nil {
			t.Fatal("gccontrol.Suspend returned nil")
		}
		t.Cleanup(s.End)
		return s
	}
	assertResumed := func(t *testing.T) {
		t.Helper()
		if got := debug.SetMemoryLimit(queryMemoryLimit); got != math.MaxInt64 {
			t.Fatalf("MemoryLimit = %d, want %d", got, int64(math.MaxInt64))
		}
		if got := debug.SetGCPercent(defaultGCPercent); got != defaultGCPercent {
			t.Fatalf("GCPercent = %d, want %d", got, defaultGCPercent)
		}
	}

	t.Run("resumeEndsImmediately", func(t *testing.T) {
		s := setup(t)
		finishSaveGCSuspension(s, resumeAfterSave, nil /* saveErr */)
		assertResumed(t)
	})

	t.Run("saveErrorEndsImmediately", func(t *testing.T) {
		s := setup(t)
		finishSaveGCSuspension(s, exitAfterSave, errors.New("save failed"))
		assertResumed(t)
	})

	t.Run("nonResumeSuccessEndsAfterDelay", func(t *testing.T) {
		origDelay := saveExitGCResumeDelay
		saveExitGCResumeDelay = 10 * time.Millisecond
		t.Cleanup(func() { saveExitGCResumeDelay = origDelay })

		s := setup(t)
		finishSaveGCSuspension(s, exitAfterSave, nil /* saveErr */)
		deadline := time.Now().Add(2 * time.Second)
		for time.Now().Before(deadline) {
			if debug.SetMemoryLimit(queryMemoryLimit) == math.MaxInt64 {
				if got := debug.SetGCPercent(defaultGCPercent); got != defaultGCPercent {
					t.Fatalf("GCPercent = %d, want %d", got, defaultGCPercent)
				}
				return
			}
			time.Sleep(5 * time.Millisecond)
		}
		t.Fatal("GC remained suspended past saveExitGCResumeDelay")
	})
}
