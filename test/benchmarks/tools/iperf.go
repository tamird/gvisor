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
	"encoding/csv"
	"fmt"
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
	cmd := []string{"iperf", "--reportstyle", "C"}
	cmd = append(cmd, "--realtime")    // Request real-time scheduling.
	cmd = append(cmd, "--len", "128K") // Length of data buffer per request.
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

// Report parses Iperf's CSV summary and reports metrics.
// The benchmark timer must be stopped.
func (i *Iperf) Report(b *testing.B, output string) {
	b.Helper()
	result, err := i.parse(output)
	if err != nil {
		b.Fatalf("failed to parse iperf CSV: %v", err)
	}
	elapsed := b.Elapsed().Seconds()
	if elapsed <= 0 {
		b.Fatalf("invalid benchmark elapsed time: %v", elapsed)
	}
	// MB/s uses reported payload and the stopped timer at Report, including
	// client container execution. It excludes work timed after reporting.
	b.ReportMetric(result.transferBytes/elapsed/1e6, "MB/s")
	// Parallel bandwidth uses the producer's aggregate interval report.
	// Neither metric measures physical wire throughput.
	ReportCustomMetric(b, result.bandwidthBytesPerSecond, "bandwidth", "bytes_per_second")
}

type iperfResult struct {
	transferBytes           float64
	bandwidthBytesPerSecond float64
}

// parse reads final TCP CSV reports: timestamp, endpoints, ID, interval,
// transferred bytes and bits/second. Negative IDs identify aggregate reports.
// MakeCmd does not request intermediate interval reports.
func (i *Iperf) parse(output string) (iperfResult, error) {
	records, err := csv.NewReader(strings.NewReader(output)).ReadAll()
	if err != nil {
		return iperfResult{}, fmt.Errorf("invalid CSV: %w", err)
	}
	parallel := max(i.Parallel, 1)
	wantRecords := parallel
	if parallel > 1 {
		wantRecords++
	}
	if len(records) != wantRecords {
		return iperfResult{}, fmt.Errorf("got %d reports, want %d", len(records), wantRecords)
	}
	seen := make(map[int]bool)
	var totalBytes, summaryBytes, summaryBits uint64
	streams := 0
	aggregate := false
	for _, record := range records {
		if len(record) != 9 {
			return iperfResult{}, fmt.Errorf("invalid TCP CSV report: %v", record)
		}
		id, err := strconv.Atoi(record[5])
		if err != nil || id == 0 || seen[id] {
			return iperfResult{}, fmt.Errorf("invalid or duplicate transfer ID: %q", record[5])
		}
		seen[id] = true
		transferred, err := strconv.ParseUint(record[7], 10, 64)
		if err != nil || transferred == 0 {
			return iperfResult{}, fmt.Errorf("invalid transferred bytes: %q", record[7])
		}
		bits, err := strconv.ParseUint(record[8], 10, 64)
		if err != nil || bits == 0 {
			return iperfResult{}, fmt.Errorf("invalid bits/second: %q", record[8])
		}
		if id < 0 {
			if aggregate || parallel == 1 {
				return iperfResult{}, fmt.Errorf("unexpected aggregate report: %v", record)
			}
			aggregate = true
			summaryBytes, summaryBits = transferred, bits
			continue
		}
		if totalBytes+transferred < totalBytes {
			return iperfResult{}, fmt.Errorf("transferred byte count overflow")
		}
		totalBytes += transferred
		streams++
		if parallel == 1 {
			summaryBytes, summaryBits = transferred, bits
		}
	}
	if streams != parallel || aggregate != (parallel > 1) || summaryBytes != totalBytes {
		return iperfResult{}, fmt.Errorf("incomplete stream or aggregate accounting")
	}
	return iperfResult{transferBytes: float64(summaryBytes), bandwidthBytesPerSecond: float64(summaryBits) / 8}, nil
}
