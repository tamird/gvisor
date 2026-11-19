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

// Binary probe checks generated protobuf counters through the coverage API.
package main

import (
	"bytes"
	"fmt"
	"os"
	"strconv"
	"strings"

	"gvisor.dev/gvisor/pkg/coverage"
	pb "gvisor.dev/gvisor/tools/bazeldefs/tests/coverage"
)

type reportBuffer struct {
	bytes.Buffer
}

func (*reportBuffer) Close() error { return nil }

func check() error {
	if len(os.Args) != 2 {
		return fmt.Errorf("expected one value argument")
	}
	input, err := strconv.ParseInt(os.Args[1], 10, 32)
	if err != nil {
		return fmt.Errorf("parse value: %w", err)
	}
	if !coverage.Available() {
		return fmt.Errorf("coverage is unavailable")
	}
	var inventory bytes.Buffer
	if err := coverage.WriteAllBlocks(&inventory); err != nil {
		return fmt.Errorf("write inventory: %w", err)
	}
	const source = "gvisor.dev/gvisor/tools/bazeldefs/tests/coverage/probe.pb.go:"
	if !strings.Contains(inventory.String(), source) {
		return fmt.Errorf("generated protobuf is missing from inventory:\n%s", &inventory)
	}
	if strings.Contains(inventory.String(), "gvisor.dev/gvisor/pkg/coverage/") {
		return fmt.Errorf("coverage runtime is instrumented:\n%s", &inventory)
	}
	coverage.ClearCoverageData()
	value := &pb.Probe{Value: int32(input)}
	if got := value.GetValue(); got != int32(input) {
		return fmt.Errorf("GetValue() = %d, want %d", got, input)
	}
	var report reportBuffer
	coverage.EnableReport(&report)
	if err := coverage.Report(); err != nil {
		return fmt.Errorf("write executed report: %w", err)
	}
	if !strings.Contains(report.String(), source) {
		return fmt.Errorf("generated protobuf is missing from executed report:\n%s", &report)
	}
	return nil
}

func main() {
	if err := check(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
