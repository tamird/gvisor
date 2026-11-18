// Copyright 2020 The gVisor Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//	http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
package tools

import (
	"math"
	"strings"
	"testing"
	"time"
)

// Captured from the canonical Iperf 2.1.9 image in TCP CSV mode.
const iperfSingle = `20261118153427,127.0.0.1,35120,127.0.0.1,40217,1,0.0-0.0,1048576,509635965
`

const iperfParallel = `20261118153428,127.0.0.1,35164,127.0.0.1,40217,3,0.0-0.0,1048576,499351628
20261118153428,127.0.0.1,35134,127.0.0.1,40217,4,0.0-0.0,1048576,502883999
20261118153428,127.0.0.1,35140,127.0.0.1,40217,1,0.0-0.0,1048576,503306413
20261118153428,127.0.0.1,35132,127.0.0.1,40217,2,0.0-0.0,1048576,502883999
20261118153428,127.0.0.1,0,127.0.0.1,40217,-1,0.0-0.0,4194304,106522006349
`

const iperfTiny = `20261118153428,127.0.0.1,35168,127.0.0.1,40217,1,0.0-0.0,1024,462589
`

var iperfReports = []struct {
	name      string
	parallel  int
	output    string
	transfer  float64
	bandwidth float64
}{
	{name: "single", output: iperfSingle, transfer: 1048576, bandwidth: 509635965.0 / 8},
	{name: "parallel", parallel: 4, output: iperfParallel, transfer: 4194304, bandwidth: 106522006349.0 / 8},
	{name: "tiny", output: iperfTiny, transfer: 1024, bandwidth: 462589.0 / 8},
}

func TestIperf(t *testing.T) {
	for _, report := range iperfReports {
		t.Run(report.name, func(t *testing.T) {
			i := Iperf{Parallel: report.parallel}
			got, err := i.parse(report.output)
			if err != nil {
				t.Fatal(err)
			}
			if got.transferBytes != report.transfer || got.bandwidthBytesPerSecond != report.bandwidth {
				t.Fatalf("got %+v, want transfer %g and bandwidth %g", got, report.transfer, report.bandwidth)
			}
		})
	}
}

func TestIperfInvalidCSV(t *testing.T) {
	for _, tc := range []struct {
		name     string
		parallel int
		output   string
	}{
		// The real refused-connection client exited zero with empty stdout.
		{name: "refused"},
		{name: "missingAggregate", parallel: 4, output: strings.Join(strings.Split(iperfParallel, "\n")[:4], "\n")},
		{name: "duplicateStream", parallel: 2, output: iperfSingle + iperfSingle + strings.Replace(iperfSingle, ",1,0.0-0.0,", ",-1,0.0-0.0,", 1)},
		{name: "wrongAggregateBytes", parallel: 4, output: strings.Replace(iperfParallel, ",4194304,", ",4194303,", 1)},
		{name: "zeroTransfer", output: strings.Replace(iperfTiny, ",1024,", ",0,", 1)},
		{name: "invalidRate", output: strings.Replace(iperfTiny, ",462589", ",NaN", 1)},
		{name: "overflow", output: strings.Replace(iperfTiny, ",1024,", ",18446744073709551616,", 1)},
		{name: "malformed", output: "not CSV"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			reported := false
			result := testing.Benchmark(func(b *testing.B) {
				b.StopTimer()
				i := Iperf{Parallel: tc.parallel}
				i.Report(b, tc.output)
				reported = true
			})
			if reported || len(result.Extra) != 0 {
				t.Fatalf("invalid CSV emitted metrics: completed %t, metrics %v", reported, result.Extra)
			}
		})
	}
}

func TestIperfReport(t *testing.T) {
	for _, report := range iperfReports {
		t.Run(report.name, func(t *testing.T) {
			var elapsed time.Duration
			result := testing.Benchmark(func(b *testing.B) {
				b.ResetTimer()
				// Give the stopped timer a positive duration. No test asserts its speed.
				time.Sleep(time.Millisecond)
				b.StopTimer()
				elapsed = b.Elapsed()
				i := Iperf{Parallel: report.parallel}
				i.Report(b, report.output)
			})
			want := report.transfer / elapsed.Seconds() / 1e6
			if got, ok := result.Extra["MB/s"]; !ok || math.Abs(got-want) > want*1e-12 {
				t.Errorf("payload/stopped-timer MB/s = %v (present %v), want %v", got, ok, want)
			}
			if got := result.Extra["bandwidth.bytes_per_second"]; got != report.bandwidth {
				t.Errorf("iperf-reported bandwidth = %v, want %v", got, report.bandwidth)
			}
		})
	}
}
