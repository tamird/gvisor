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
	"encoding/json"
	"fmt"
	"math"
	"testing"
)

// heyOutputTemplate reports the producer's statistics and request errors as JSON.
const heyOutputTemplate = `{"requests":{{.NumRes}},"seconds":{{.Total.Seconds}},"requests_per_second":{{.Rps}},"average_latency_seconds":{{if .ErrorDist}}null{{else}}{{.Average}}{{end}},"errors":{{jsonify .ErrorDist}},"status_codes":{{jsonify .StatusCodeDist}}}`

// Hey is for the client application 'hey'.
type Hey struct {
	Requests    int // Note: requests cannot be less than concurrency.
	Concurrency int
	Doc         string
}

// MakeCmd returns a 'hey' command.
func (h *Hey) MakeCmd(host string, port int) []string {
	c := h.Concurrency
	if c > h.Requests {
		c = h.Requests
	}
	return []string{
		"hey",
		"-o", heyOutputTemplate,
		"-n", fmt.Sprintf("%d", h.Requests),
		"-c", fmt.Sprintf("%d", c),
		fmt.Sprintf("http://%s:%d/%s", host, port, h.Doc),
	}
}

// Report parses Hey's JSON summary and reports metrics.
func (h *Hey) Report(b *testing.B, output string) {
	b.Helper()
	var report struct {
		RequestsPerSecond float64        `json:"requests_per_second"`
		AverageLatency    *float64       `json:"average_latency_seconds"`
		Errors            map[string]int `json:"errors"`
	}
	if err := json.Unmarshal([]byte(output), &report); err != nil {
		b.Fatalf("failed to parse hey JSON: %v", err)
	}
	if report.Errors == nil || len(report.Errors) != 0 {
		b.Fatalf("hey request error accounting is missing or reports errors: %s", output)
	}
	if report.RequestsPerSecond <= 0 || math.IsNaN(report.RequestsPerSecond) || math.IsInf(report.RequestsPerSecond, 0) {
		b.Fatalf("invalid hey request rate: %v", report.RequestsPerSecond)
	}
	if report.AverageLatency == nil || *report.AverageLatency < 0 || math.IsNaN(*report.AverageLatency) || math.IsInf(*report.AverageLatency, 0) {
		b.Fatalf("invalid hey average latency: %v", report.AverageLatency)
	}
	ReportCustomMetric(b, report.RequestsPerSecond, "requests_per_second" /*metric name*/, "QPS" /*unit*/)
	ReportCustomMetric(b, *report.AverageLatency, "average_latency" /*metric name*/, "s" /*unit*/)
}
