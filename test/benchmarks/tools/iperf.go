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
	"testing"
)

// Iperf is for the client side of iperf3.
type Iperf struct {
	Num      int // Requested transfer size across all streams, in KiB.
	Parallel int // Number of parallel TCP streams.
}

// MakeCmd returns an iperf3 client command.
func (i *Iperf) MakeCmd(host string, port int) []string {
	cmd := []string{"iperf3"}
	cmd = append(cmd, "--json")           // Report aggregate sender throughput.
	cmd = append(cmd, "--length", "128K") // Data buffer size per write.
	n := max(i.Num, 1)                    // iperf3 requires a positive total across all streams.
	cmd = append(cmd, "--bytes", fmt.Sprintf("%dK", n))
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
