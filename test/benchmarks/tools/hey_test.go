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

import "testing"

// These summaries were captured from the canonical Hey v0.1.5 image.
const heySuccess = `{"requests":4,"seconds":0.002128464,"requests_per_second":1879.2894782340693,"average_latency_seconds":0.00101637925,"errors":{},"status_codes":{"200":4}}`

const heyNotFound = `{"requests":4,"seconds":0.001695339,"requests_per_second":2359.41012387493,"average_latency_seconds":0.0007628817499999999,"errors":{},"status_codes":{"404":4}}`

const heyTimeout = `{"requests":4,"seconds":2.001163301,"requests_per_second":1.9988373752412723,"average_latency_seconds":null,"errors":{"Get \"http://127.0.0.1:45711/timeout\": context deadline exceeded (Client.Timeout exceeded while awaiting headers)":4},"status_codes":null}`

// TestHeyReport checks the public reporting boundary, including failed requests.
func TestHeyReport(t *testing.T) {
	for _, tc := range []struct {
		name         string
		output       string
		wantReported bool
		wantQPS      float64
		wantAverage  float64
	}{
		{name: "success", output: heySuccess, wantReported: true, wantQPS: 1879.2894782340693, wantAverage: 0.00101637925},
		{name: "notFound", output: heyNotFound, wantReported: true, wantQPS: 2359.41012387493, wantAverage: 0.0007628817499999999},
		{name: "timeout", output: heyTimeout},
		{name: "missingErrors", output: `{"requests_per_second":10,"average_latency_seconds":0.1}`},
		{name: "missingLatency", output: `{"requests_per_second":10,"errors":{}}`},
		{name: "zeroRate", output: `{"requests_per_second":0,"average_latency_seconds":0.1,"errors":{}}`},
		{name: "negativeLatency", output: `{"requests_per_second":10,"average_latency_seconds":-1,"errors":{}}`},
		{name: "malformed", output: `{`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			reported := false
			result := testing.Benchmark(func(b *testing.B) {
				b.StopTimer()
				hey := Hey{}
				hey.Report(b, tc.output)
				reported = true
			})
			if reported != tc.wantReported {
				t.Fatalf("Report completed: got %t, want %t", reported, tc.wantReported)
			}
			if reported {
				if got := result.Extra["requests_per_second.QPS"]; got != tc.wantQPS {
					t.Errorf("requests per second: got %g, want %g", got, tc.wantQPS)
				}
				if got := result.Extra["average_latency.s"]; got != tc.wantAverage {
					t.Errorf("average latency: got %g, want %g", got, tc.wantAverage)
				}
			} else if len(result.Extra) != 0 {
				t.Fatalf("Report emitted metrics for invalid results: %v", result.Extra)
			}
		})
	}
}
