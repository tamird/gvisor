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
	"fmt"
	"reflect"
	"strings"
	"testing"
)

// TestIperf checks the aggregate summary instead of an individual stream or
// an interval, and rejects incomplete or failed iperf3 results.
func TestIperf(t *testing.T) {
	for _, tc := range []struct {
		name      string
		output    string
		bandwidth float64
		error     string
	}{
		{
			name: "aggregate",
			output: `{
				"intervals": [{"sum": {"bits_per_second": 999}}],
				"end": {
					"streams": [
						{"sender": {"bits_per_second": 1000000}},
						{"sender": {"bits_per_second": 2000000}}
					],
					"sum_sent": {"bytes": 375000, "bits_per_second": 3000000},
					"sum_received": {"bytes": 375000, "bits_per_second": 2900000}
				}
			}`,
			bandwidth: 375000,
		},
		{
			name:   "remote error",
			output: `{"error": "unable to connect to server", "end": {}}`,
			error:  "iperf3 failed: unable to connect to server",
		},
		{
			name:   "missing summary",
			output: `{"end": {"streams": [{"sender": {"bits_per_second": 1000000}}]}}`,
			error:  "missing positive iperf3 sender summary",
		},
		{
			name:   "no transfer",
			output: `{"end": {"sum_sent": {"bytes": 0, "bits_per_second": 0}}}`,
			error:  "missing positive iperf3 sender summary",
		},
		{
			name:   "invalid JSON",
			output: "Server listening on 5001",
			error:  "invalid iperf3 result",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			i := Iperf{}
			got, err := i.bandwidth(tc.output)
			if tc.error != "" {
				if err == nil || !strings.Contains(err.Error(), tc.error) {
					t.Fatalf("bandwidth() error = %v, want %q", err, tc.error)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if want := tc.bandwidth; got != want {
				t.Errorf("bandwidth() = %v, want %v", got, want)
			}
		})
	}
}

func TestIperfMakeCmd(t *testing.T) {
	// --bytes is a total across streams, so parallelism must not divide it.
	for _, parallel := range []int{0, 4, 16, 64} {
		i := Iperf{Num: 128, Parallel: parallel}
		got := i.MakeCmd("server", 5001)
		want := []string{"iperf3", "--json", "--length", "128K", "--bytes", "128K", "--client", "server", "--port", "5001"}
		if parallel > 0 {
			want = append(want, "--parallel", fmt.Sprint(parallel))
		}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("MakeCmd(Parallel=%d) = %q, want %q", parallel, got, want)
		}
	}
}
