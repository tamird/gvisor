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

package runtimes

import (
	"context"
	"flag"
	"fmt"
	"os"
	"strconv"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/test/dockerutil"
)

func TestMain(m *testing.M) {
	flag.Parse()
	os.Exit(dockerutil.RunTests(m.Run))
}

// This fork-only diagnostic executes the released test body, while reporting
// its measured loop time rather than requiring the two-second assertion to pass.
// CPU and fault deltas cover the evaluated body, including array setup and
// output; loop_seconds is the test's own measurement of the loop.
const phpTiming = `
$source = file_get_contents("Zend/tests/concat/concat_003.phpt");
if (hash("sha256", $source) !== "be2ac2b32e73c3c7030c08f777d5878306b0cbfa3b3e56f22bfd64ea8af24c8a") {
    throw new Exception("unexpected concat_003.phpt source");
}
$sections = explode("--FILE--\n", $source, 2);
$code = explode("--EXPECT--\n", $sections[1], 2)[0];
$code = str_replace("array_fill(0, 220000,", "array_fill(0, " . $argv[1] . ",", $code, $count);
if ($count !== 1) {
    throw new Exception("expected one input-size substitution");
}
$before = getrusage();
eval("?>" . $code);
$after = getrusage();
echo json_encode([
    "php" => PHP_VERSION,
    "items" => (int)$argv[1],
    "loop_seconds" => $t,
    "user_seconds" => $after["ru_utime.tv_sec"] - $before["ru_utime.tv_sec"] + ($after["ru_utime.tv_usec"] - $before["ru_utime.tv_usec"]) / 1000000,
    "system_seconds" => $after["ru_stime.tv_sec"] - $before["ru_stime.tv_sec"] + ($after["ru_stime.tv_usec"] - $before["ru_stime.tv_usec"]) / 1000000,
    "minor_faults" => $after["ru_minflt"] - $before["ru_minflt"],
    "major_faults" => $after["ru_majflt"] - $before["ru_majflt"],
], JSON_THROW_ON_ERROR), "\n";
`

func TestPHPConcatenationTiming(t *testing.T) {
	ctx := t.Context()
	native := dockerutil.MakeNativeContainer(ctx, t)
	sandbox := dockerutil.MakeContainer(ctx, t)
	containers := []*dockerutil.Container{native, sandbox}
	for _, c := range containers {
		t.Cleanup(func() {
			ctx, cancel := context.WithTimeout(context.WithoutCancel(t.Context()), 15*time.Second)
			defer cancel()
			if err := c.CleanUp(ctx); err != nil {
				t.Errorf("container cleanup: %v", err)
			}
		})
		if err := c.Spawn(ctx, dockerutil.RunOpts{Image: "runtimes/php8.5.11"}, "sleep", "infinity"); err != nil {
			t.Fatal(err)
		}
	}
	for iteration := range 10 {
		for _, size := range []int{110000, 220000, 440000} {
			for order := range 2 {
				index := (iteration + order) % 2
				output, err := containers[index].Exec(ctx, dockerutil.ExecOpts{}, "/root/php-8.5.11/sapi/cli/php", "-n", "-r", phpTiming, strconv.Itoa(size))
				if err != nil {
					t.Fatalf("runtime=%d iteration=%d size=%d: %v\n%s", index, iteration, size, err, output)
				}
				fmt.Printf("PHP_TIMING runtime=%s iteration=%d size=%d\n%s\n", []string{"native", "directfs"}[index], iteration, size, output)
			}
		}
	}
}
