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
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"testing"
)

// Quote latency so Hey's NaN when all requests fail remains valid JSON.
const heyOutputTemplate = `{
  "requests_per_second": {{.Rps}},
  "average_latency_seconds": "{{.Average}}",
  "results": {{.NumRes}},
  "status_codes": {{jsonify .StatusCodeDist}},
  "errors": {{jsonify .ErrorDist}}
}`

type heyReport struct {
	Results           int            `json:"results"`
	StatusCodes       map[int]int    `json:"status_codes"`
	RequestsPerSecond float64        `json:"requests_per_second"`
	AverageLatency    *float64       `json:"average_latency_seconds,string"`
	Errors            map[string]int `json:"errors"`
}

// Hey is for the client application 'hey'.
type Hey struct {
	Requests       int
	Concurrency    int
	Doc            string
	ExpectedStatus int // Zero selects HTTP 200.
}

// MakeCmd returns a 'hey' command.
func (h *Hey) MakeCmd(host string, port int) []string {
	c := min(h.Concurrency, h.Requests)
	return []string{
		"hey",
		"-o", heyOutputTemplate,
		"-n", fmt.Sprintf("%d", h.Requests),
		"-c", fmt.Sprintf("%d", c),
		fmt.Sprintf("http://%s:%d/%s", host, port, h.Doc),
	}
}

// Report reports metrics from the output of heyOutputTemplate.
func (h *Hey) Report(b *testing.B, output []byte) {
	b.Helper()
	b.Logf("Hey requested=%d concurrency=%d elapsed before cleanup=%s; producer output=%s", h.Requests, min(h.Concurrency, h.Requests), b.Elapsed(), output)
	var report heyReport
	decoder := json.NewDecoder(bytes.NewReader(output))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&report); err != nil {
		b.Fatalf("failed to parse hey output: %v\n%s", err, output)
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		b.Fatalf("unexpected trailing hey output: %s", output)
	}
	if report.Errors == nil || len(report.Errors) != 0 {
		b.Fatalf("hey request error accounting is missing or reports errors: %s", output)
	}
	c := min(h.Concurrency, h.Requests)
	if c <= 0 {
		b.Fatalf("invalid hey request count or concurrency: %d, %d", h.Requests, h.Concurrency)
	}
	// Hey v0.1.5 runs N/C requests per worker and drops the remainder.
	requests := h.Requests / c * c
	if report.Results != requests {
		b.Fatalf("hey completed %d results, want %d: %s", report.Results, requests, output)
	}
	expectedStatus := h.ExpectedStatus
	if expectedStatus == 0 {
		expectedStatus = http.StatusOK
	}
	// Hey v0.1.5 retains statuses for at most one million successful results.
	// Larger runs check a sample, not the status of every response.
	// https://github.com/rakyll/hey/blob/v0.1.5/requester/report.go
	statusCount := min(requests, 1000000)
	if len(report.StatusCodes) != 1 || report.StatusCodes[expectedStatus] != statusCount {
		b.Fatalf("hey status counts %v, want %d HTTP %d responses: %s", report.StatusCodes, statusCount, expectedStatus, output)
	}
	if report.RequestsPerSecond <= 0 {
		b.Fatalf("invalid hey request rate: %v", report.RequestsPerSecond)
	}
	if report.AverageLatency == nil || *report.AverageLatency < 0 || math.IsNaN(*report.AverageLatency) || math.IsInf(*report.AverageLatency, 0) {
		b.Fatalf("invalid hey average latency in output: %s", output)
	}
	ReportCustomMetric(b, report.RequestsPerSecond, "requests_per_second" /*metric name*/, "QPS" /*unit*/)
	ReportCustomMetric(b, *report.AverageLatency, "average_latency" /*metric name*/, "s" /*unit*/)
}
