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

// Binary coverage_bounds reports canonical median bounds without rounding.
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"os"
	"slices"
	"sort"

	"github.com/bazelbuild/rules_go/go/runfiles"
	"golang.org/x/perf/benchfmt"
	"golang.org/x/perf/benchmath"
	"golang.org/x/perf/benchunit"
)

type key struct {
	Benchmark string
	Unit      string
}

type report struct {
	File       string            `json:"file"`
	SHA256     string            `json:"sha256"`
	Benchmark  string            `json:"benchmark"`
	Unit       string            `json:"unit"`
	Iterations []int             `json:"iterations"`
	Values     []float64         `json:"values"`
	Summary    benchmath.Summary `json:"summary"`
}

func read(name string) ([]report, error) {
	path, err := runfiles.Rlocation(name)
	if err != nil {
		return nil, err
	}
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	return parse(name, file)
}

func parse(name string, input io.Reader) ([]report, error) {
	hash := sha256.New()
	reader := benchfmt.NewReader(io.TeeReader(input, hash), name)
	var header [3]string
	headerSet := false
	groups := make(map[key]*report)
	for reader.Scan() {
		switch row := reader.Result().(type) {
		case *benchfmt.SyntaxError:
			return nil, row
		case *benchfmt.Result:
			var current [3]string
			for i, key := range []string{"pkg", "goos", "goarch"} {
				pos, ok := row.ConfigIndex(key)
				if !ok || len(row.Config[pos].Value) == 0 {
					if key == "pkg" {
						continue
					}
					return nil, fmt.Errorf("missing %s header in %s", key, name)
				}
				current[i] = string(row.Config[pos].Value)
			}
			if headerSet && current != header {
				return nil, fmt.Errorf("mixed package/OS/architecture headers in %s", name)
			}
			header, headerSet = current, true
			if row.Iters <= 0 {
				return nil, fmt.Errorf("nonpositive iteration count in %s", row.Name)
			}
			seen := make(map[string]bool)
			for _, value := range row.Values {
				value.Value, value.Unit = benchunit.Tidy(value.Value, value.Unit)
				if value.Unit != "sec/op" && value.Unit != "execution_time.s" && value.Unit != "execution_time_per_loop.s" {
					continue
				}
				if value.Value <= 0 || math.IsNaN(value.Value) || math.IsInf(value.Value, 0) {
					return nil, fmt.Errorf("invalid positive timing in %s", row.Name)
				}
				if seen[value.Unit] {
					return nil, fmt.Errorf("duplicate timing unit %s in %s", value.Unit, row.Name)
				}
				seen[value.Unit] = true
				k := key{string(row.Name), value.Unit}
				if groups[k] == nil {
					groups[k] = &report{File: name, Benchmark: k.Benchmark, Unit: k.Unit}
				}
				groups[k].Values = append(groups[k].Values, value.Value)
				groups[k].Iterations = append(groups[k].Iterations, row.Iters)
			}
		}
	}
	if err := reader.Err(); err != nil {
		return nil, err
	}
	if len(groups) == 0 {
		return nil, fmt.Errorf("no timing observations in %s", name)
	}
	keys := make([]key, 0, len(groups))
	for k := range groups {
		keys = append(keys, k)
	}
	sort.Slice(keys, func(i, j int) bool {
		if keys[i].Benchmark != keys[j].Benchmark {
			return keys[i].Benchmark < keys[j].Benchmark
		}
		return keys[i].Unit < keys[j].Unit
	})
	out := make([]report, 0, len(keys))
	for _, k := range keys {
		r := groups[k]
		r.SHA256 = hex.EncodeToString(hash.Sum(nil))
		r.Summary = benchmath.AssumeNothing.Summary(benchmath.NewSample(slices.Clone(r.Values), &benchmath.DefaultThresholds), 0.995)
		if len(r.Summary.Warnings) != 0 {
			return nil, fmt.Errorf("canonical summary warning for %s: %v", k.Benchmark, r.Summary.Warnings)
		}
		out = append(out, *r)
	}
	return out, nil
}

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "expected retained benchmark runfiles")
		os.Exit(1)
	}
	encoder := json.NewEncoder(os.Stdout)
	for _, name := range os.Args[1:] {
		rows, err := read(name)
		if err == nil {
			err = encoder.Encode(rows)
		}
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
	}
}
