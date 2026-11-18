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
	"math"
	"regexp"
	"strconv"
	"strings"
	"testing"
)

// Iperf is for the client side of `iperf`.
type Iperf struct {
	Num      int // Data to send in KiB, before division among streams.
	Parallel int // Number of parallel threads.
}

// MakeCmd returns a iperf client command.
func (i *Iperf) MakeCmd(host string, port int) []string {
	cmd := []string{"iperf"}
	cmd = append(cmd, "--format", "K") // Output in KBytes.
	cmd = append(cmd, "--realtime")    // Measured in realtime.
	cmd = append(cmd, "--len", "128K") // Length of the write buffer.
	n := i.Num
	if i.Parallel > 0 {
		// Must be at least 1, otherwise iperf will complain about having nothing to transmit.
		n = max(i.Num/i.Parallel, 1)
	}
	cmd = append(cmd, "--num", fmt.Sprintf("%dK", n)) // Data to send per stream.
	cmd = append(cmd, "--client", host)
	cmd = append(cmd, "--port", fmt.Sprintf("%d", port))
	if i.Parallel > 0 {
		cmd = append(cmd, "--parallel", fmt.Sprintf("%d", i.Parallel))
	}
	return cmd
}

// Report parses the final iperf summary and reports metrics.
// The benchmark timer must be stopped.
func (i *Iperf) Report(b *testing.B, output string) {
	b.Helper()
	result, err := i.parse(output)
	if err != nil {
		b.Fatalf("failed to parse iperf summary: %v", err)
	}
	elapsed := b.Elapsed().Seconds()
	if elapsed <= 0 {
		b.Fatalf("invalid benchmark elapsed time: %v", elapsed)
	}
	// MB/s uses the payload and the stopped timer at Report, including client
	// container execution. It excludes any work timed after reporting.
	b.ReportMetric(result.transferBytes/elapsed/1e6, "MB/s")
	// For parallel streams, bandwidth is iperf's aggregate summary over its
	// reported interval. Neither metric measures physical wire throughput.
	ReportCustomMetric(b, result.bandwidthBytesPerSecond, "bandwidth", "bytes_per_second")
}

type iperfResult struct {
	transferBytes           float64
	bandwidthBytesPerSecond float64
}

// parse selects the final aggregate summary for parallel streams, or the
// final single-stream summary otherwise. MakeCmd requests KBytes output.
func (i *Iperf) parse(data string) (iperfResult, error) {
	id := `\s*\d+`
	if i.Parallel > 1 {
		id = `SUM`
	}
	re := regexp.MustCompile(`(?m)^\[` + id + `\]([^\n]*)$`)
	matches := re.FindAllStringSubmatch(data, -1)
	if len(matches) == 0 {
		return iperfResult{}, fmt.Errorf("missing final iperf summary: %s", data)
	}
	fields := strings.Fields(matches[len(matches)-1][1])
	if len(fields) != 6 || fields[1] != "sec" || fields[3] != "KBytes" || fields[5] != "KBytes/sec" {
		return iperfResult{}, fmt.Errorf("invalid final iperf summary: %s", data)
	}
	transfer, err := parseIperfKBytes(fields[2])
	if err != nil {
		return iperfResult{}, err
	}
	bandwidth, err := parseIperfKBytes(fields[4])
	if err != nil {
		return iperfResult{}, err
	}
	return iperfResult{transferBytes: transfer, bandwidthBytesPerSecond: bandwidth}, nil
}

func parseIperfKBytes(value string) (float64, error) {
	kbytes, err := strconv.ParseFloat(value, 64)
	if err != nil {
		return 0, fmt.Errorf("invalid iperf value %q: %w", value, err)
	}
	bytes := kbytes * 1024
	if bytes <= 0 || math.IsNaN(bytes) || math.IsInf(bytes, 0) {
		return 0, fmt.Errorf("invalid iperf value %q", value)
	}
	return bytes, nil
}
