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

import "testing"

// TestIperf checks final single-stream and aggregate producer reports.
func TestIperf(t *testing.T) {
	single := `
------------------------------------------------------------
Client connecting to 10.138.15.215, TCP port 32779
TCP window size: 45.0 KByte (default)
------------------------------------------------------------
[  3] local 10.138.15.216 port 32866 connected with 10.138.15.215 port 32779
[ ID] Interval       Transfer     Bandwidth
[  3]  0.0-10.0 sec  459520 KBytes  45900 KBytes/sec
`
	// Captured from the canonical iperf 2.1.5 image. Use the producer's
	// aggregate rate, whose reported interval differs from individual streams.
	parallel := `------------------------------------------------------------
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
	for _, test := range []struct {
		name     string
		parallel int
		output   string
		want     float64
		wantErr  bool
	}{
		{name: "single", output: single, want: 45900},
		{name: "one parallel stream", parallel: 1, output: single, want: 45900},
		{name: "final single summary", output: "[  3] 0.0-1.0 sec 1 KBytes 1 KBytes/sec\n" + single, want: 45900},
		{name: "parallel", parallel: 4, output: parallel, want: 19140187},
		{name: "final aggregate", parallel: 4, output: "[SUM] 0.0-1.0 sec 1 KBytes 1 KBytes/sec\n" + parallel, want: 19140187},
		{name: "missing aggregate", parallel: 4, output: single, wantErr: true},
		{name: "malformed final aggregate", parallel: 4, output: parallel + "[SUM] invalid summary\n", wantErr: true},
		{name: "zero aggregate", parallel: 4, output: "[SUM] 0.0000-0.0001 sec 4.00 KBytes 0.000 KBytes/sec\n"},
	} {
		t.Run(test.name, func(t *testing.T) {
			i := Iperf{Parallel: test.parallel}
			got, err := i.bandwidth(test.output)
			if (err != nil) != test.wantErr {
				t.Fatalf("bandwidth() = %v, %v; want error %t", got, err, test.wantErr)
			}
			if !test.wantErr && got != test.want {
				t.Errorf("bandwidth() = %v, want %v", got, test.want)
			}
		})
	}
}
