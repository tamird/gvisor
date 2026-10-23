// Copyright 2020 The gVisor Authors.
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

package tools

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Iperf is for the client side of iperf3.
type Iperf struct {
	Num             int // Total number of KiB to send across all streams.
	Parallel        int // Number of parallel TCP streams.
	DurationSeconds int // Qualification-only timed transfer instead of a byte budget.
	OmitSeconds     int // Initial interval excluded from iperf statistics.
}

// MakeCmd returns an iperf3 client command.
func (i *Iperf) MakeCmd(host string, port int) []string {
	cmd := []string{"iperf3"}
	cmd = append(cmd, "--json")           // Report aggregate sender throughput.
	cmd = append(cmd, "--length", "128K") // Data buffer size per write.
	if i.DurationSeconds > 0 {
		cmd = append(cmd, "--time", fmt.Sprint(i.DurationSeconds), "--omit", fmt.Sprint(i.OmitSeconds))
	} else {
		n := max(i.Num, 1) // iperf3 requires a positive total across all streams.
		cmd = append(cmd, "--bytes", fmt.Sprintf("%dK", n))
	}
	cmd = append(cmd, "--client", host)
	cmd = append(cmd, "--port", fmt.Sprintf("%d", port))
	if i.Parallel > 0 {
		cmd = append(cmd, "--parallel", fmt.Sprintf("%d", i.Parallel))
	}
	return cmd
}

// Report parses output from iperf3 and reports aggregate sender throughput.
func (i *Iperf) Report(b *testing.B, output string) {
	b.Helper()
	bandwidth, err := i.bandwidth(output)
	if err != nil {
		b.Fatalf("failed to parse bandwidth from %s: %v", output, err)
	}
	// Qualification-only observation after the benchmark stops its timer.
	observation := struct {
		Name            string
		Iterations      int
		Parallel        int
		DurationSeconds int
		OmitSeconds     int
		Output          json.RawMessage
	}{b.Name(), i.Num, i.Parallel, i.DurationSeconds, i.OmitSeconds, json.RawMessage(output)}
	data, err := json.Marshal(observation)
	if err != nil {
		b.Fatalf("failed to retain iperf3 observation: %v", err)
	}
	directory := os.Getenv("TEST_UNDECLARED_OUTPUTS_DIR")
	if directory == "" {
		b.Fatal("TEST_UNDECLARED_OUTPUTS_DIR is required for the pilot")
	}
	name := fmt.Sprintf("iperf-%s-%d.json", strings.ReplaceAll(b.Name(), "/", "_"), i.Num)
	file, err := os.OpenFile(filepath.Join(directory, name), os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0644)
	if err != nil {
		b.Fatalf("failed to create iperf3 observation: %v", err)
	}
	if _, err := file.Write(data); err != nil {
		file.Close()
		b.Fatalf("failed to write iperf3 observation: %v", err)
	}
	if err := file.Close(); err != nil {
		b.Fatalf("failed to close iperf3 observation: %v", err)
	}
	if i.DurationSeconds == 0 {
		b.SetBytes(1024) // Each byte-mode iteration requests one KiB.
	}
	ReportCustomMetric(b, bandwidth, "bandwidth" /*metric name*/, "bytes_per_second" /*unit*/)
}

// bandwidth returns aggregate sender throughput in bytes per second.
func (i *Iperf) bandwidth(data string) (float64, error) {
	var result struct {
		Error string `json:"error"`
		End   struct {
			Sent *struct {
				Bytes         uint64  `json:"bytes"`
				BitsPerSecond float64 `json:"bits_per_second"`
			} `json:"sum_sent"`
		} `json:"end"`
	}
	if err := json.Unmarshal([]byte(data), &result); err != nil {
		return 0, fmt.Errorf("invalid iperf3 result: %w", err)
	}
	if result.Error != "" {
		return 0, fmt.Errorf("iperf3 failed: %s", result.Error)
	}
	if result.End.Sent == nil || result.End.Sent.Bytes == 0 || result.End.Sent.BitsPerSecond <= 0 {
		return 0, fmt.Errorf("missing positive iperf3 sender summary")
	}
	return result.End.Sent.BitsPerSecond / 8, nil
}
