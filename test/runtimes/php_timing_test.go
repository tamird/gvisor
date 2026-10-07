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
	"path/filepath"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/test/dockerutil"
)

func TestMain(m *testing.M) {
	flag.Parse()
	os.Exit(dockerutil.RunTests(m.Run))
}

// Execute the released body unchanged. Its two-second assertion is reported,
// not used as a qualification gate under profiling.
const phpTiming = `
$source = file_get_contents("Zend/tests/concat/concat_003.phpt");
if (hash("sha256", $source) !== "be2ac2b32e73c3c7030c08f777d5878306b0cbfa3b3e56f22bfd64ea8af24c8a") {
    throw new Exception("unexpected concat_003.phpt source");
}
$sections = explode("--FILE--\n", $source, 2);
$code = explode("--EXPECT--\n", $sections[1], 2)[0];
eval("?>" . $code);
echo json_encode([
    "php" => PHP_VERSION,
    "items" => 220000,
    "loop_seconds" => $t,
    "loop_start_unix_seconds" => $time,
    "pid" => getmypid(),
], JSON_THROW_ON_ERROR), "\n";
`

func TestPHPConcatenationProfile(t *testing.T) {
	outputDir := os.Getenv("TEST_UNDECLARED_OUTPUTS_DIR")
	if outputDir == "" {
		t.Fatal("profile requires TEST_UNDECLARED_OUTPUTS_DIR")
	}
	profileDir := filepath.Join(outputDir, "php-profile")
	for name, value := range map[string]string{
		"pprof-cpu":      "true",
		"pprof-duration": "60s",
		"pprof-dir":      profileDir,
	} {
		if err := flag.Set(name, value); err != nil {
			t.Fatalf("profile flag %s: %v", name, err)
		}
	}
	ctx := t.Context()
	sandbox := dockerutil.MakeContainer(ctx, t)
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.WithoutCancel(t.Context()), 15*time.Second)
		defer cancel()
		if err := sandbox.CleanUp(ctx); err != nil {
			t.Errorf("container cleanup: %v", err)
		}
		// The fixture logs profiler failures; require its actual output after
		// cleanup has stopped and joined the collector. Parsing is external.
		profile := filepath.Join(profileDir, dockerutil.Runtime(), t.Name(), "runtimes/php8.5.11/cpu.pprof")
		info, err := os.Stat(profile)
		if err != nil {
			t.Fatalf("CPU profile: %v", err)
		}
		if !info.Mode().IsRegular() || info.Size() == 0 {
			t.Fatalf("CPU profile is not a nonempty regular file: %v", info)
		}
		fmt.Printf("PHP_PROFILE bytes=%d\n", info.Size())
	})
	if err := sandbox.Spawn(ctx, dockerutil.RunOpts{Image: "runtimes/php8.5.11"}, "sleep", "infinity"); err != nil {
		t.Fatal(err)
	}
	// The existing collector starts after ContainerStart. These execs include
	// PHP startup and array setup; loop_seconds covers only the original loop.
	for iteration := range 10 {
		output, err := sandbox.Exec(ctx, dockerutil.ExecOpts{}, "/root/php-8.5.11/sapi/cli/php", "-n", "-r", phpTiming)
		if err != nil {
			t.Fatalf("iteration=%d: %v\n%s", iteration, err, output)
		}
		fmt.Printf("PHP_TIMING runtime=directfs iteration=%d size=220000\n%s\n", iteration, output)
	}
}
