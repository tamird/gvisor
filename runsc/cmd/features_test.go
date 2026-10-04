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

package cmd

import (
	"context"
	"encoding/json"
	"os"
	"testing"

	"github.com/google/subcommands"
	"github.com/opencontainers/runtime-spec/specs-go/features"
)

func TestFeatures(t *testing.T) {
	// A pipe can fill before Execute returns because the output is read below.
	output, err := os.CreateTemp(t.TempDir(), "features")
	if err != nil {
		t.Fatalf("Creating output file: %v", err)
	}
	originalStdout := os.Stdout
	os.Stdout = output
	t.Cleanup(func() {
		os.Stdout = originalStdout
		if err := output.Close(); err != nil {
			t.Errorf("Closing output file: %v", err)
		}
	})

	cmd := &Features{}
	if status := cmd.Execute(context.Background(), nil); status != subcommands.ExitSuccess {
		t.Fatalf("Execute returned %v, want %v", status, subcommands.ExitSuccess)
	}
	data, err := os.ReadFile(output.Name())
	if err != nil {
		t.Fatalf("Reading output file: %v", err)
	}

	var feat features.Features
	if err := json.Unmarshal(data, &feat); err != nil {
		t.Fatalf("Failed to parse JSON output: %v. Output was:\n%s", err, data)
	}
}
