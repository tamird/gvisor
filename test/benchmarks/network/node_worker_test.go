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
package network

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/test/dockerutil"
	"gvisor.dev/gvisor/test/benchmarks/harness"
)

func TestNodeProducerWithWorker(t *testing.T) {
	harness.StartWorker(t)
	t.Run("TestNodeProducer", TestNodeProducer)
}

func TestHeyReportInterface(t *testing.T) {
	harness.StartWorker(t)
	machine, err := harness.GetMachine()
	if err != nil {
		t.Fatal(err)
	}
	defer machine.CleanUp()
	cases := []struct {
		name       string
		status     int
		advertised int
		written    int
	}{
		{"full", http.StatusOK, 4206, 4206},
		{"short", http.StatusOK, 46, 46},
		{"http_error", http.StatusInternalServerError, 7, 7},
		{"truncated", http.StatusOK, 4206, 46},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(t.Context(), 20*time.Second)
			defer cancel()
			var served, written atomic.Int64
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				served.Add(1)
				w.Header().Set("Content-Length", fmt.Sprint(tc.advertised))
				w.WriteHeader(tc.status)
				n, _ := io.WriteString(w, strings.Repeat("x", tc.written))
				written.Add(int64(n))
			}))
			defer server.Close()
			client := machine.GetNativeContainer(ctx, t)
			defer client.CleanUp(context.WithoutCancel(ctx))
			const template = `{"responses":{{.NumRes}},"statuses":{{jsonify .StatusCodeDist}},"advertised_bytes":{{.SizeTotal}},"errors":{{jsonify .ErrorDist}}}`
			output, err := client.Run(ctx, dockerutil.RunOpts{Image: "benchmarks/hey", NetworkMode: "host"},
				"hey", "-o", template, "-n", "5", "-c", "1", "-t", "2", server.URL)
			if err != nil {
				t.Fatalf("pinned Hey interface: %v: %s", err, output)
			}
			var report struct {
				Responses       *int           `json:"responses"`
				Statuses        map[string]int `json:"statuses"`
				AdvertisedBytes *int64         `json:"advertised_bytes"`
				Errors          map[string]int `json:"errors"`
			}
			decoder := json.NewDecoder(strings.NewReader(output))
			decoder.DisallowUnknownFields()
			if err := decoder.Decode(&report); err != nil {
				t.Fatalf("pinned Hey JSON: %v: %s", err, output)
			}
			if err := decoder.Decode(new(any)); err != io.EOF {
				t.Fatalf("trailing pinned Hey output: %s", output)
			}
			if report.Responses == nil || *report.Responses != 5 || report.AdvertisedBytes == nil || report.Errors == nil || served.Load() != 5 || written.Load() != 5*int64(tc.written) {
				t.Fatalf("incomplete interface accounting: served=%d written=%d report=%s", served.Load(), written.Load(), output)
			}
			if tc.name != "truncated" && (len(report.Errors) != 0 || len(report.Statuses) != 1 || report.Statuses[fmt.Sprint(tc.status)] != 5 || *report.AdvertisedBytes != 5*int64(tc.advertised)) {
				t.Fatalf("unexpected pinned Hey accounting: %s", output)
			}
			record, err := json.Marshal(struct {
				Case        string          `json:"hey_interface"`
				Served      int64           `json:"served_responses"`
				ServerBytes int64           `json:"server_body_bytes"`
				Report      json.RawMessage `json:"report"`
			}{tc.name, served.Load(), written.Load(), json.RawMessage(output)})
			if err != nil {
				t.Fatal(err)
			}
			t.Logf("%s", record)
		})
	}
}
