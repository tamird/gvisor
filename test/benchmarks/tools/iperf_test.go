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

// TestIperf checks the Iperf parsers on sample output.
func TestIperf(t *testing.T) {
	sampleData := `
------------------------------------------------------------
Client connecting to 10.138.15.215, TCP port 32779
TCP window size: 45.0 KByte (default)
------------------------------------------------------------
[  3] local 10.138.15.216 port 32866 connected with 10.138.15.215 port 32779
[ ID] Interval       Transfer     Bandwidth
[  3]  0.0-10.0 sec  459520 KBytes  45900 KBytes/sec
`
	i := Iperf{}
	bandwidth, err := i.bandwidth(sampleData)
	if err != nil || bandwidth != 45900 {
		t.Fatalf("failed with: %v and %f", err, bandwidth)
	}
}

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
