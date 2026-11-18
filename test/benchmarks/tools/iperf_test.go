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
	"testing"
	"time"
)

// Captured from the canonical iperf 2.1.5 image. The aggregate interval
// differs from individual stream intervals in the parallel report.
const iperfSingle = `------------------------------------------------------------
Client connecting to 127.0.0.1, TCP port 19876
TCP window size: 2.50 MByte (default)
------------------------------------------------------------
[  1] local 127.0.0.1 port 39046 connected with 127.0.0.1 port 19876
[ ID] Interval       Transfer     Bandwidth
[  1] 0.0000-0.0163 sec  1024 KBytes  62749 KBytes/sec
`

const iperfParallel = `------------------------------------------------------------
Client connecting to 127.0.0.1, TCP port 19876
TCP window size: 2.50 MByte (default)
------------------------------------------------------------
[  4] local 127.0.0.1 port 39086 connected with 127.0.0.1 port 19876
[  2] local 127.0.0.1 port 39062 connected with 127.0.0.1 port 19876
[  1] local 127.0.0.1 port 39060 connected with 127.0.0.1 port 19876
[  3] local 127.0.0.1 port 39082 connected with 127.0.0.1 port 19876
[ ID] Interval       Transfer     Bandwidth
[  2] 0.0000-0.0167 sec  1024 KBytes  61306 KBytes/sec
[  1] 0.0000-0.0168 sec  1024 KBytes  61021 KBytes/sec
[  3] 0.0000-0.0167 sec  1024 KBytes  61336 KBytes/sec
[  4] 0.0000-0.0168 sec  1024 KBytes  61087 KBytes/sec
[SUM] 0.0000-0.0002 sec  4096 KBytes  19140187 KBytes/sec
[ CT] final connect times (min/avg/max/stdev) = 0.043/0.130/0.310/0.122 ms (tot/err) = 4/0
`

const iperfTiny = `------------------------------------------------------------
Client connecting to 127.0.0.1, TCP port 19876
TCP window size: 2.50 MByte (default)
------------------------------------------------------------
[  1] local 127.0.0.1 port 39092 connected with 127.0.0.1 port 19876
[ ID] Interval       Transfer     Bandwidth
[  1] 0.0000-0.0172 sec  1.00 KBytes  58.0 KBytes/sec
`

var iperfReports = []struct {
	name      string
	parallel  int
	output    string
	transfer  float64
	bandwidth float64
}{
	{name: "single", output: iperfSingle, transfer: 1024 * 1024, bandwidth: 62749 * 1024},
	{name: "parallel", parallel: 4, output: iperfParallel, transfer: 4096 * 1024, bandwidth: 19140187 * 1024},
	{name: "tiny", output: iperfTiny, transfer: 1024, bandwidth: 58 * 1024},
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
				t.Fatalf("got %+v, want transfer %v bytes and bandwidth %v bytes/sec", got, report.transfer, report.bandwidth)
			}
		})
	}
}

func TestIperfFinalSummary(t *testing.T) {
	i := Iperf{}
	result, err := i.parse("[  3] 0.0-1.0 sec 1 KBytes 1 KBytes/sec\n[  3] 0.0-10.0 sec 459520 KBytes 45900 KBytes/sec\n")
	if err != nil {
		t.Fatal(err)
	}
	if result.transferBytes != 459520*1024 || result.bandwidthBytesPerSecond != 45900*1024 {
		t.Fatalf("got %+v, want the final summary", result)
	}
}

func TestIperfInvalidSummary(t *testing.T) {
	for _, test := range []struct {
		name     string
		parallel int
		output   string
	}{
		{name: "empty"},
		{name: "missing aggregate", parallel: 4, output: iperfSingle},
		{name: "invalid final row", output: iperfTiny + "[  1] invalid final summary\n"},
		{name: "zero transfer", output: "[  1] 0.0-1.0 sec 0 KBytes 1 KBytes/sec\n"},
		{name: "nonfinite final rate", output: iperfTiny + "[  1] 0.0-1.0 sec 1 KBytes NaN KBytes/sec\n"},
		{name: "overflow", output: "[  1] 0.0-1.0 sec 1e308 KBytes 1 KBytes/sec\n"},
		{name: "wrong units", output: "[  1] 0.0-1.0 sec 1 MBytes 1 MBytes/sec\n"},
	} {
		t.Run(test.name, func(t *testing.T) {
			i := Iperf{Parallel: test.parallel}
			if result, err := i.parse(test.output); err == nil {
				t.Fatalf("accepted invalid summary: %+v", result)
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
