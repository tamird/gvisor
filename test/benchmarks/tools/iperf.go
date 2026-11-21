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
	"fmt"
	"regexp"
	"strconv"
	"testing"
)

// Iperf is for the client side of `iperf`.
type Iperf struct {
	Num      int // Requested transfer size across all streams, in KiB.
	Parallel int // Number of parallel threads.
}

// MakeCmd returns a iperf client command.
func (i *Iperf) MakeCmd(host string, port int) []string {
	cmd := []string{"iperf"}
	cmd = append(cmd, "--format", "K") // Output in KBytes.
	cmd = append(cmd, "--realtime")    // Measured in realtime.
	cmd = append(cmd, "--len", "128K") // Length of data buffer per request.
	n := i.Num
	if i.Parallel > 0 {
		// Must be at least 1, otherwise iperf will complain about having nothing to transmit.
		n = max(i.Num/i.Parallel, 1)
	}
	cmd = append(cmd, "--num", fmt.Sprintf("%dK", n)) // Number of requests to send.
	cmd = append(cmd, "--client", host)
	cmd = append(cmd, "--port", fmt.Sprintf("%d", port))
	if i.Parallel > 0 {
		cmd = append(cmd, "--parallel", fmt.Sprintf("%d", i.Parallel))
	}
	return cmd
}

// Report records iperf's client-reported bandwidth.
func (i *Iperf) Report(b *testing.B, output string) {
	b.Helper()
	b.Logf("Iperf N=%d elapsed before cleanup=%s; producer output=%q", b.N, b.Elapsed(), output)
	// Parse bandwidth and report it.
	bW, err := i.bandwidth(output)
	if err != nil {
		b.Fatalf("failed to parse bandwitdth from %s: %v", output, err)
	}
	ReportCustomMetric(b, bW*1024, "bandwidth" /*metric name*/, "bytes_per_second" /*unit*/)
}

// bandwidth returns the client's final reported rate in KBytes/sec.
// Parallel transfers use the producer's aggregate summary.
func (i *Iperf) bandwidth(data string) (float64, error) {
	id := `\s*\d+`
	if i.Parallel > 1 {
		id = `SUM`
	}
	rows := regexp.MustCompile(`(?m)^\[`+id+`\][^\n]*$`).FindAllString(data, -1)
	if len(rows) == 0 {
		return 0, fmt.Errorf("missing iperf bandwidth summary: %s", data)
	}
	summary := rows[len(rows)-1]
	match := regexp.MustCompile(`\s+(\d+\.?\d*)\s+KBytes/sec(?:\s|$)`).FindStringSubmatch(summary)
	if len(match) == 0 {
		return 0, fmt.Errorf("invalid iperf bandwidth summary: %s", summary)
	}
	return strconv.ParseFloat(match[1], 64)
}
