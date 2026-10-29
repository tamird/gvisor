// Copyright 2026 The gVisor Authors.
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

package main

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strings"

	"github.com/google/pprof/profile"
)

const statePath = "gvisor.dev/gvisor/pkg/state."

func read(path string) *profile.Profile {
	f, err := os.Open(path)
	if err != nil {
		panic(err)
	}
	defer f.Close()
	p, err := profile.Parse(f)
	if err != nil {
		panic(err)
	}
	if err := p.CheckValid(); err != nil {
		panic(err)
	}
	return p
}

func values(p *profile.Profile, name string) int {
	for i, t := range p.SampleType {
		if t.Type == name {
			return i
		}
	}
	panic("missing pprof value: " + name)
}

func stack(s *profile.Sample) (string, map[string]bool) {
	symbols := make(map[string]bool)
	leaf := "[unmapped]"
	for i, loc := range s.Location {
		for j, line := range loc.Line {
			if line.Function == nil || line.Function.Name == "" {
				continue
			}
			name := line.Function.Name
			if i == 0 && j == 0 {
				leaf = name
			}
			symbols[name] = true
		}
	}
	return leaf, symbols
}

type ranked struct {
	Name  string
	Value int64
}
type counter struct {
	Total      int64
	Flat       map[string]int64
	Cumulative map[string]int64
}

func inc(c **counter, raw int64, leaf string, names map[string]bool) {
	if *c == nil {
		*c = &counter{Flat: map[string]int64{}, Cumulative: map[string]int64{}}
	}
	b := *c
	b.Total += raw
	b.Flat[leaf] += raw
	for n := range names {
		b.Cumulative[n] += raw
	}
}

func top(m map[string]int64) []ranked {
	var rows []ranked
	for n, v := range m {
		if v != 0 {
			rows = append(rows, ranked{n, v})
		}
	}
	sort.Slice(rows, func(i, j int) bool {
		if rows[i].Value == rows[j].Value {
			return rows[i].Name < rows[j].Name
		}
		return rows[i].Value > rows[j].Value
	})
	if len(rows) > 24 {
		rows = rows[:24]
	}
	return rows
}

type summary struct {
	Total            int64
	Flat, Cumulative []ranked
}

func compact(c map[string]*counter) map[string]summary {
	out := map[string]summary{}
	for k, b := range c {
		out[k] = summary{b.Total, top(b.Flat), top(b.Cumulative)}
	}
	return out
}

func update(c map[string]*counter, key string, val int64, leaf string, stack map[string]bool) {
	b := c[key]
	inc(&b, val, leaf, stack)
	c[key] = b
}

func main() {
	for _, path := range os.Args[1:] {
		p := read(path)
		cpu := p.PeriodType != nil && p.PeriodType.Type == "cpu"
		kinds := []string{"alloc_objects", "alloc_space"}
		if cpu {
			kinds = []string{"samples"}
			if p.PeriodType.Unit != "nanoseconds" || p.Period <= 0 {
				panic("missing CPU period")
			}
		}
		group := map[string]*counter{}
		nearest := map[string]map[string]int64{}
		fixtures := map[string]bool{}
		for _, s := range p.Sample {
			leaf, names := stack(s)
			fix, gc := false, false
			for name := range names {
				if strings.Contains(name, "test/benchmarks/checkpointfile") && strings.Contains(name, ".BenchmarkCheckpointFile.") {
					fix = true
					fixtures[name] = true
				}
				if strings.HasPrefix(name, "runtime.gc") || strings.HasPrefix(name, "runtime.bgsweep") || strings.HasPrefix(name, "runtime.bgscavenge") {
					gc = true
				}
			}
			class := "other"
			switch {
			case names[statePath+"Save"] && fix:
				class = "fixture-save"
			case names[statePath+"Save"]:
				class = "save-without-fixture"
			case names[statePath+"Load"]:
				class = "load"
			case gc:
				class = "gc-or-sweep"
			case fix:
				class = "fixture-outside-save"
			case leaf == "[unmapped]":
				class = "unmapped"
			}
			firstState := ""
			if class == "fixture-save" {
				for _, loc := range s.Location {
					for _, line := range loc.Line {
						if line.Function != nil && strings.HasPrefix(line.Function.Name, statePath) {
							firstState = line.Function.Name
							break
						}
					}
					if firstState != "" {
						break
					}
				}
				if firstState == "" {
					panic("no state frame")
				}
			}
			for _, kind := range kinds {
				value := s.Value[values(p, kind)]
				if value < 0 {
					panic("negative profile value")
				}
				update(group, class+"/"+kind, value, leaf, names)
				if class == "fixture-save" {
					if nearest[kind] == nil {
						nearest[kind] = map[string]int64{}
					}
					nearest[kind][firstState] += value
				}
			}
		}
		out := struct {
			File     string
			Period   int64
			CPU      bool
			Groups   map[string]summary
			Nearest  map[string]map[string]int64
			Fixtures map[string]bool
		}{path, p.Period, cpu, compact(group), nearest, fixtures}
		raw, err := json.Marshal(out)
		if err != nil {
			panic(err)
		}
		fmt.Printf("SAVE_PROFILE %s\n", raw)
	}
}
