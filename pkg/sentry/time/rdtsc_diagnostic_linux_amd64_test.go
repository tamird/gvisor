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

package time

import (
	"context"
	"fmt"
	"math"
	"runtime"
	"testing"
	"time"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/sentry/hostcpu"
	"gvisor.dev/gvisor/pkg/sync"
)

type diagnosticSample struct {
	cycles TSCValue
	err    error
}

// TestOrderedTSC tests raw readings in this worker, not calibrated clock state
// or the historical worker that emitted a clock warning. Channel handoffs and
// GetCPU's RDTSCP add ordering; a pass cannot exclude failures without them.
func TestOrderedTSC(t *testing.T) {
	const iterations = 10000
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	var workers sync.WaitGroup
	defer func() {
		cancel()
		workers.Wait()
	}()

	var allowed unix.CPUSet
	if err := unix.SchedGetaffinity(0, &allowed); err != nil {
		t.Fatalf("read allowed CPUs: %v", err)
	}
	// Bound thread and pair counts without silently sampling only part of the
	// allowed CPU set. The failing Firecracker VM exposed four vCPUs.
	if n := allowed.Count(); n < 2 || n > 8 {
		t.Fatalf("incomplete diagnostic: allowed CPU count %d, want 2..8", n)
	}
	var cpus []int
	for cpu := 0; len(cpus) < allowed.Count(); cpu++ {
		if allowed.IsSet(cpu) {
			cpus = append(cpus, cpu)
		}
	}
	t.Logf("allowed CPUs=%v, iterations per ordered pair=%d", cpus, iterations)

	requests := make([]chan struct{}, len(cpus))
	ready := make(chan error, 1)
	results := make(chan diagnosticSample, 1)
	for i, cpu := range cpus {
		requests[i] = make(chan struct{})
		workers.Go(func() {
			runtime.LockOSThread()
			defer runtime.UnlockOSThread()
			var original unix.CPUSet
			if err := unix.SchedGetaffinity(0, &original); err != nil {
				ready <- fmt.Errorf("CPU %d: read original affinity: %w", cpu, err)
				return
			}
			defer func() {
				if err := unix.SchedSetaffinity(0, &original); err != nil {
					t.Errorf("CPU %d: restore original affinity: %v", cpu, err)
				}
			}()
			var affinity unix.CPUSet
			affinity.Set(cpu)
			if err := unix.SchedSetaffinity(0, &affinity); err != nil {
				ready <- fmt.Errorf("pin CPU %d: %w", cpu, err)
				return
			}
			var actual unix.CPUSet
			if err := unix.SchedGetaffinity(0, &actual); err != nil {
				ready <- fmt.Errorf("CPU %d: read pinned affinity: %w", cpu, err)
				return
			}
			if actual != affinity {
				ready <- fmt.Errorf("CPU %d: affinity is %v, want %v", cpu, actual, affinity)
				return
			}
			ready <- nil
			for {
				select {
				case <-ctx.Done():
					return
				case <-requests[i]:
				}
				before := hostcpu.GetCPU()
				cycles := Rdtsc()
				after := hostcpu.GetCPU()
				sample := diagnosticSample{cycles: cycles}
				if before != uint32(cpu) || after != uint32(cpu) {
					sample.err = fmt.Errorf("CPU %d: observed CPUs before=%d, after=%d", cpu, before, after)
				}
				select {
				case results <- sample:
				case <-ctx.Done():
					return
				}
			}
		})
		select {
		case err := <-ready:
			if err != nil {
				t.Fatal(err)
			}
		case <-ctx.Done():
			t.Fatalf("incomplete diagnostic during affinity setup: %v", ctx.Err())
		}
	}

	read := func(i int) TSCValue {
		t.Helper()
		select {
		case requests[i] <- struct{}{}:
		case <-ctx.Done():
			t.Fatalf("incomplete diagnostic requesting CPU %d: %v", cpus[i], ctx.Err())
		}
		select {
		case sample := <-results:
			if sample.err != nil {
				t.Fatal(sample.err)
			}
			return sample.cycles
		case <-ctx.Done():
			t.Fatalf("incomplete diagnostic reading CPU %d: %v", cpus[i], ctx.Err())
			return 0
		}
	}
	for i, from := range cpus {
		for j, to := range cpus {
			minDelta := TSCValue(math.MaxInt64)
			negative := 0
			for iteration := 0; iteration < iterations; iteration++ {
				// Receive the first completed reading before requesting the
				// second, including same-CPU controls when i == j.
				first := read(i)
				second := read(j)
				delta := second - first
				minDelta = min(minDelta, delta)
				if delta < 0 {
					if negative == 0 {
						t.Errorf("CPU %d -> %d iteration %d: first=%d, second=%d, delta=%d", from, to, iteration, first, second, delta)
					}
					negative++
				}
			}
			t.Logf("CPU %d -> %d: samples=%d, min_delta=%d, negatives=%d", from, to, iterations, minDelta, negative)
		}
	}
}
