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

// TestHey checks the Hey parsers on sample output.
func TestHey(t *testing.T) {
	sampleData := `
	Summary:
          Total:	2.2391 secs
          Slowest:	1.6292 secs
          Fastest:	0.0066 secs
          Average:	0.5351 secs
          Requests/sec:	89.3202

          Total data:	841200 bytes
          Size/request:	4206 bytes

        Response time histogram:
          0.007 [1]	|
          0.169 [0]	|
          0.331 [149]	|■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■
          0.493 [0]	|
          0.656 [0]	|
          0.818 [0]	|
          0.980 [0]	|
          1.142 [0]	|
          1.305 [0]	|
          1.467 [49]	|■■■■■■■■■■■■■
          1.629 [1]	|


        Latency distribution:
          10% in 0.2149 secs
          25% in 0.2449 secs
          50% in 0.2703 secs
          75% in 1.3315 secs
          90% in 1.4045 secs
          95% in 1.4232 secs
          99% in 1.4362 secs

        Details (average, fastest, slowest):
          DNS+dialup:	0.0002 secs, 0.0066 secs, 1.6292 secs
          DNS-lookup:	0.0000 secs, 0.0000 secs, 0.0000 secs
          req write:	0.0000 secs, 0.0000 secs, 0.0012 secs
          resp wait:	0.5225 secs, 0.0064 secs, 1.4346 secs
          resp read:	0.0122 secs, 0.0001 secs, 0.2006 secs

        Status code distribution:
          [200]	200 responses
	`
	hey := Hey{}
	want := 89.3202
	got, err := hey.parseRequestsPerSecond(sampleData)
	if err != nil {
		t.Fatalf("failed to parse request per second with: %v", err)
	} else if got != want {
		t.Fatalf("got: %f, want: %f", got, want)
	}

	want = 0.5351
	got, err = hey.parseAverageLatency(sampleData)
	if err != nil {
		t.Fatalf("failed to parse average latency with: %v", err)
	} else if got != want {
		t.Fatalf("got: %f, want: %f", got, want)
	}
}

// Captured from the canonical Hey image against native and Sentry HTTPD.
const heySuccess = `
Summary:
  Total:	0.1610 secs
  Slowest:	0.0011 secs
  Fastest:	0.0001 secs
  Average:	0.0002 secs
  Requests/sec:	6209.5882


Response time histogram:
  0.000 [1]	|
  0.000 [943]	|■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■
  0.000 [33]	|■
  0.000 [16]	|■
  0.000 [3]	|
  0.001 [1]	|
  0.001 [0]	|
  0.001 [1]	|
  0.001 [0]	|
  0.001 [1]	|
  0.001 [1]	|


Latency distribution:
  10%% in 0.0001 secs
  25%% in 0.0001 secs
  50%% in 0.0002 secs
  75%% in 0.0002 secs
  90%% in 0.0002 secs
  95%% in 0.0002 secs
  99%% in 0.0004 secs

Details (average, fastest, slowest):
  DNS+dialup:	0.0000 secs, 0.0000 secs, 0.0003 secs
  DNS-lookup:	0.0000 secs, 0.0000 secs, 0.0001 secs
  req write:	0.0000 secs, 0.0000 secs, 0.0001 secs
  resp wait:	0.0001 secs, 0.0001 secs, 0.0010 secs
  resp read:	0.0000 secs, 0.0000 secs, 0.0004 secs

Status code distribution:
  [200]	1000 responses



`

const heyNotfound = `
Summary:
  Total:	5.0208 secs
  Slowest:	5.0149 secs
  Fastest:	0.0054 secs
  Average:	1.0698 secs
  Requests/sec:	19.9171

  Total data:	26800 bytes
  Size/request:	268 bytes

Response time histogram:
  0.005 [1]	|■
  0.506 [78]	|■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■
  1.007 [0]	|
  1.508 [0]	|
  2.009 [0]	|
  2.510 [0]	|
  3.011 [0]	|
  3.512 [0]	|
  4.013 [0]	|
  4.514 [0]	|
  5.015 [21]	|■■■■■■■■■■■


Latency distribution:
  10%% in 0.0100 secs
  25%% in 0.0153 secs
  50%% in 0.0272 secs
  75%% in 0.0377 secs
  90%% in 5.0106 secs
  95%% in 5.0120 secs
  99%% in 5.0149 secs

Details (average, fastest, slowest):
  DNS+dialup:	0.0022 secs, 0.0000 secs, 0.0055 secs
  DNS-lookup:	0.0006 secs, 0.0000 secs, 0.0070 secs
  req write:	0.0003 secs, 0.0000 secs, 0.0008 secs
  resp wait:	1.0663 secs, 0.0039 secs, 5.0113 secs
  resp read:	0.0000 secs, 0.0000 secs, 0.0004 secs

Status code distribution:
  [404]	100 responses



`

const heyTimeout = `
Summary:
  Total:	20.0430 secs
  Slowest:	15.1927 secs
  Fastest:	0.0282 secs
  Average:	4.1957 secs
  Requests/sec:	49.8928


Response time histogram:
  0.028 [1]	|
  1.545 [320]	|■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■
  3.061 [0]	|
  4.578 [0]	|
  6.094 [73]	|■■■■■■■■■
  7.610 [0]	|
  9.127 [0]	|
  10.643 [74]	|■■■■■■■■■
  12.160 [0]	|
  13.676 [0]	|
  15.193 [74]	|■■■■■■■■■


Latency distribution:
  10%% in 0.0466 secs
  25%% in 0.0687 secs
  50%% in 0.1041 secs
  75%% in 10.1345 secs
  90%% in 15.1586 secs
  95%% in 15.1735 secs
  99%% in 15.1865 secs

Details (average, fastest, slowest):
  DNS+dialup:	0.0117 secs, 0.0000 secs, 0.0428 secs
  DNS-lookup:	0.0011 secs, 0.0000 secs, 0.0112 secs
  req write:	0.0019 secs, 0.0000 secs, 0.0105 secs
  resp wait:	4.1539 secs, 0.0050 secs, 15.1570 secs
  resp read:	0.0001 secs, 0.0000 secs, 0.0004 secs

Status code distribution:
  [200]	542 responses

Error distribution:
  [458]	Get "http://server:80/latin10k.txt": context deadline exceeded (Client.Timeout exceeded while awaiting headers)

`

// TestHeyReport checks the public reporting boundary against client output.
func TestHeyReport(t *testing.T) {
	for _, tc := range []struct {
		name         string
		output       string
		wantReported bool
		wantQPS      float64
		wantAverage  float64
	}{
		{name: "success", output: heySuccess, wantReported: true, wantQPS: 6209.5882, wantAverage: 0.0002},
		{name: "notfound", output: heyNotfound, wantReported: true, wantQPS: 19.9171, wantAverage: 1.0698},
		{name: "timeout", output: heyTimeout},
		{name: "emptyErrorSection", output: "Average: 0.1 secs\nRequests/sec: 10\n  Error distribution: \n"},
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
			}
			if !reported && len(result.Extra) != 0 {
				t.Fatalf("Report emitted metrics for request errors: %v", result.Extra)
			}
		})
	}
}
