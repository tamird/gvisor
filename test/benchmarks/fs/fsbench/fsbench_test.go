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

package fsbench

import (
	"context"
	"os"
	"testing"

	"gvisor.dev/gvisor/test/benchmarks/harness"
)

func TestFuseSmoke(t *testing.T) {
	machine, err := harness.GetMachine()
	if err != nil {
		t.Fatal(err)
	}
	defer machine.CleanUp()

	iterations := 0
	passed := false
	result := testing.Benchmark(func(b *testing.B) {
		RunWithDifferentFilesystems(context.Background(), b, machine, FSBenchmark{
			Image:   "basic/alpine",
			WorkDir: "/tmp",
			// Require FUSE_SUPER_MAGIC instead of passing on the underlying tmpfs.
			RunCmd: []string{"sh", "-c", `test "$(stat -f -c %t .)" = 65735546 &&
printf fuse-smoke > probe && test "$(cat probe)" = fuse-smoke && echo FUSE_SMOKE_OK`},
			WantOutput: "FUSE_SMOKE_OK",
			Variants:   []Variant{{fsType: harness.FuseFS}},
			Callback: func(b *testing.B, _ string) {
				if b.N != 1 {
					b.Fatal("FUSE smoke requires -test.benchtime=1x")
				}
				iterations++
			},
		})
		passed = !b.Failed()
	})
	if !passed || iterations != 1 {
		t.Fatalf("FUSE benchmark setup failed after %d iterations: %s", iterations, result)
	}
}

func TestMain(m *testing.M) {
	harness.Init()
	os.Exit(m.Run())
}
