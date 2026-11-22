// Copyright 2026 The gVisor Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package main

import (
	"math"
	"runtime"
	"strings"
	"testing"
)

func TestSchedule(t *testing.T) {
	for _, preflight := range []bool{false, true} {
		orders := schedule(preflight)
		want := 25
		if preflight {
			want = 3
		}
		if len(orders) != want || orders[0] != "AB" {
			t.Fatalf("schedule(%t) = %v", preflight, orders)
		}
		for i, order := range orders[1:] {
			if want := []string{"AB", "BA"}[i%2]; order != want {
				t.Errorf("pair %d = %q, want %q", i, order, want)
			}
		}
	}
}

func TestParse(t *testing.T) {
	header := "goos: " + runtime.GOOS + "\ngoarch: " + runtime.GOARCH + "\ncpu: test\n"
	row := "BenchmarkOne 1 10 ns/op 0 B/op 0 allocs/op\n"
	got, err := parse([]byte(header+row), []string{"One"}, true)
	if err != nil {
		t.Fatal(err)
	}
	if metrics := got["One"].Metrics; len(metrics) != 3 || math.Abs(metrics["sec/op"]-1e-8) > 1e-23 || metrics["B/op"] != 0 || metrics["allocs/op"] != 0 {
		t.Fatalf("metrics = %v", metrics)
	}
	for _, text := range []string{
		"", // Missing row.
		row + row,
		strings.ReplaceAll(row, "One", "Other"),
		strings.ReplaceAll(row, "One 1", "One 2"),
		strings.ReplaceAll(row, "10 ns/op", "oops ns/op"),
		strings.ReplaceAll(row, "10 ns/op", "0 ns/op"),
	} {
		if _, err := parse([]byte(header+text), []string{"One"}, true); err == nil {
			t.Errorf("accepted invalid preflight output %q", text)
		}
	}
}

func TestContrasts(t *testing.T) {
	for _, test := range []struct {
		name                string
		a, b                func(int) float64
		ratio, drift, order float64
	}{
		{"equal", func(int) float64 { return 100 }, func(int) float64 { return 100 }, 1, 1, 1},
		{"ratio", func(int) float64 { return 100 }, func(int) float64 { return 110 }, 1.1, 1, 1},
		{"drift", func(i int) float64 { return 100 + 2*float64(i/12) }, func(i int) float64 { return 100 + 2*float64(i/12) }, 1, 1.02, 1},
		{"order", func(i int) float64 { return 100 + 2*float64(i%2) }, func(i int) float64 { return 102 - 2*float64(i%2) }, (1.02 + 100.0/102) / 2, 1, 1.0404},
	} {
		t.Run(test.name, func(t *testing.T) {
			a, b := make([]float64, 24), make([]float64, 24)
			for i := range a {
				a[i], b[i] = test.a(i), test.b(i)
			}
			got, err := summarize(a, b)
			if err != nil {
				t.Fatal(err)
			}
			for i, interval := range []interval{got.Ratio, got.Drift, got.Order} {
				want := []float64{test.ratio, test.drift, test.order}[i]
				if math.Abs(interval.Median-want) > 1e-12 || interval.Confidence < .95 {
					t.Errorf("contrast %d = %+v, want median %v and confidence >= 0.95", i, interval, want)
				}
			}
		})
	}
}
