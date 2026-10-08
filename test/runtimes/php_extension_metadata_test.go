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
	"strings"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/test/dockerutil"
)

func TestMain(m *testing.M) {
	flag.Parse()
	os.Exit(dockerutil.RunTests(m.Run))
}

// This fork-only check observes the upstream harness's missing-extension
// result. It does not claim execution of the PostgreSQL test body.
func TestPHPPDOExtensionMetadata(t *testing.T) {
	const test = "ext/pdo_pgsql/tests/transations_deprecations.phpt"
	const report = "/tmp/php-extension-results.txt"
	for _, native := range []bool{true, false} {
		mode := "goferfs"
		makeContainer := dockerutil.MakeContainer
		if native {
			mode = "native"
			makeContainer = dockerutil.MakeNativeContainer
		}
		t.Run(mode, func(t *testing.T) {
			ctx := t.Context()
			c := makeContainer(ctx, t)
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
			output, err := c.Exec(ctx, dockerutil.ExecOpts{}, "sha256sum", test)
			if err != nil || output != "81a030b6ba3f969e42179a018927b61ec9c3366d39cd03337e1eacf32b21a638  "+test+"\n" {
				t.Fatalf("corrected PHPT identity: %v\n%s", err, output)
			}
			output, err = c.Exec(ctx, dockerutil.ExecOpts{}, "sapi/cli/php", "-n", "-r", `echo PHP_VERSION, " ", (int)extension_loaded("pdo"), " ", (int)extension_loaded("pdo_pgsql"), " ", (int)defined("PDO::PGSQL_TRANSACTION_IDLE"), "\n";`)
			if err != nil || output != "8.5.11 1 0 0\n" {
				t.Fatalf("extension availability: %v\n%s", err, output)
			}
			output, err = c.Exec(ctx, dockerutil.ExecOpts{}, "make", "test", "TESTS=--no-color -W "+report+" "+test)
			fmt.Printf("PHP_METADATA runtime=%s\n%s\n", mode, output)
			if err != nil || !strings.Contains(output, "Required extension missing: pdo_pgsql") {
				t.Fatalf("upstream missing-extension result: %v\n%s", err, output)
			}
			output, err = c.Exec(ctx, dockerutil.ExecOpts{}, "cat", report)
			fmt.Printf("PHP_METADATA_REPORT runtime=%s\n%s\n", mode, output)
			if err != nil || strings.TrimSpace(output) != "SKIPPED\t/root/php-8.5.11/"+test {
				t.Fatalf("upstream result record: %v\n%s", err, output)
			}
		})
	}
}
