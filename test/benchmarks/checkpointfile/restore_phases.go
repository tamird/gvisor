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

package checkpointfile

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/cpuid"
	"gvisor.dev/gvisor/pkg/sentry/kernel"
	"gvisor.dev/gvisor/pkg/sentry/pgalloc"
	"gvisor.dev/gvisor/pkg/sentry/platform"
	"gvisor.dev/gvisor/pkg/sentry/platform/systrap"
	"gvisor.dev/gvisor/pkg/sentry/state/checkpointfiles"
	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/state/statefile"
	"gvisor.dev/gvisor/pkg/state/wire"
	"gvisor.dev/gvisor/pkg/test/dockerutil"
	"gvisor.dev/gvisor/pkg/test/testutil"
	"gvisor.dev/gvisor/test/benchmarks/harness"
)

// BenchmarkCaptureRestorePhases captures once in a process separate from every
// measured child. Only the raw Sentry state and the host-resource descriptions
// needed to load it are handed to the children.
func BenchmarkCaptureRestorePhases(b *testing.B) {
	if b.N != 1 || !testutil.IsCheckpointSupported() || dockerutil.Runtime() != "runsc" {
		b.Fatal("requires a single checkpoint-enabled runsc capture")
	}
	dir := os.Getenv("RESTORE_PHASE_DIR")
	if dir == "" {
		b.Fatal("missing private checkpoint directory")
	}
	machine, err := harness.GetMachine()
	if err != nil {
		b.Fatal(err)
	}
	defer machine.CleanUp()
	data, sizes, names, mounts := checkpointNginx(context.Background(), b, machine)
	meta, err := json.Marshal(checkpointSpec{Names: names, Mounts: mounts})
	if err != nil {
		b.Fatal(err)
	}
	for name, contents := range map[string][]byte{checkpointfiles.StateFileName: data, checkpointSpecName: meta} {
		if err := os.WriteFile(filepath.Join(dir, name), contents, 0600); err != nil {
			b.Fatal(err)
		}
	}
	fmt.Printf("phase-capture digest=%x state=%d pages=%d pagemeta=%d\n", sha256.Sum256(data), len(data), sizes[checkpointfiles.PagesFileName], sizes[checkpointfiles.PagesMetadataFileName])
}

// BenchmarkRestorePhases runs only in a fresh child. It never saves an object,
// calls CollectStats or preloads a Go type before the first timed state.Load.
func BenchmarkRestorePhases(b *testing.B) {
	if b.N != 1 {
		b.Fatal("requires one invocation with exactly two restores")
	}
	data, _, names, mounts, err := readCheckpointDir(os.Getenv("RESTORE_PHASE_DIR"))
	if err != nil {
		b.Fatal(err)
	}
	digest := sha256.Sum256(data)
	r, _, err := statefile.NewReader(io.NopCloser(bytes.NewReader(data)), nil)
	if err != nil {
		b.Fatal(err)
	}
	for _, name := range []string{"cpuid", "kernel"} {
		size, object, err := state.ReadHeader(&wire.Reader{Reader: r})
		if err != nil || !object || size == 0 || size > uint64(len(data))*32 {
			b.Fatalf("frame %s: %d, %v, %v", name, size, object, err)
		}
		h := sha256.New()
		if _, err := io.CopyN(h, r, int64(size)); err != nil {
			b.Fatal(err)
		}
		fmt.Printf("phase-frame root=%s bytes=%d sha=%x\n", name, size, h.Sum(nil))
	}
	if err := r.Verify(); err != nil {
		b.Fatal(err)
	}
	if err := r.Close(); err != nil {
		b.Fatal(err)
	}

	ctx := restoreContext(b, names, mounts)
	sys, err := systrap.New(platform.Options{})
	if err != nil {
		b.Fatal(err)
	}
	measureAlloc := os.Getenv("CALLBACK_STUDY_HEAP") == "1"
	for pass := 1; pass <= 2; pass++ {
		restoreCtx, cleanup := withHostFDs(b, ctx, names)
		r, _, err := statefile.NewReader(io.NopCloser(bytes.NewReader(data)), nil)
		if err != nil {
			b.Fatal(err)
		}
		var fs cpuid.FeatureSet
		start := time.Now()
		cpuidStats, err := state.Load(restoreCtx, r, &fs)
		if err != nil {
			b.Fatal(err)
		}
		cpuidNS := time.Since(start).Nanoseconds()
		k := &kernel.Kernel{Platform: stubPlatform{sys}}
		k.SetMemoryFile(pgalloc.MemoryFileFromContext(restoreCtx))
		var heapDone func(int)
		if measureAlloc {
			heapDone = callbackHeapBracket()
		}
		start = time.Now()
		kernelStats, err := state.Load(restoreCtx, r, k)
		if err != nil {
			b.Fatal(err)
		}
		kernelNS := time.Since(start).Nanoseconds()
		if heapDone != nil {
			heapDone(pass)
		}
		if err := r.Verify(); err != nil {
			b.Fatal(err)
		}
		if err := r.Close(); err != nil {
			b.Fatal(err)
		}
		ns := k.RootPIDNamespace()
		var tasks []string
		for _, t := range ns.Tasks() {
			tasks = append(tasks, fmt.Sprintf("%d:%s", ns.IDOfTask(t), t.Name()))
		}
		sort.Strings(tasks)
		if len(tasks) < workers+1 || sha256.Sum256(data) != digest {
			b.Fatal("restored task set or input identity changed")
		}
		var features uint32
		for bit := 0; bit < 32; bit++ {
			if fs.HasFeature(cpuid.Feature(bit)) {
				features |= 1 << bit
			}
		}
		taskJSON, err := json.Marshal(tasks)
		if err != nil {
			b.Fatal(err)
		}
		fmt.Printf("phase-restore pid=%d pass=%d digest=%x tasks=%s features=%08x cpuid_ns=%d kernel_ns=%d\n", os.Getpid(), pass, digest, taskJSON, features, cpuidNS, kernelNS)
		for _, root := range []struct {
			name  string
			stats *state.Stats
		}{{"cpuid", &cpuidStats}, {"kernel", &kernelStats}} {
			if s, ok := any(root.stats).(interface{ RestorePhaseSample() ([8][4]int64, [3]int) }); ok {
				phases, shape := s.RestorePhaseSample()
				val, err := json.Marshal(phases)
				if err != nil {
					b.Fatal(err)
				}
				sh, err := json.Marshal(shape)
				if err != nil {
					b.Fatal(err)
				}
				fmt.Printf("phase-trace pass=%d root=%s shape=%s phases=%s\n", pass, root.name, sh, val)
			}
		}
		cleanup()
	}
}

// callbackHeapBracket records process-wide allocation counters without forcing GC.
func callbackHeapBracket() func(int) {
	var before, after runtime.MemStats
	done := func(pass int) {
		runtime.ReadMemStats(&after)
		fmt.Printf("callback-heap pass=%d bytes=%d mallocs=%d\n", pass, after.TotalAlloc-before.TotalAlloc, after.Mallocs-before.Mallocs)
	}
	runtime.ReadMemStats(&before)
	return done
}
