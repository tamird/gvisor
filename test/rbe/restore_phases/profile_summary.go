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
	"path/filepath"
	"sort"
	"strings"

	"github.com/google/pprof/profile"
)

const load = "gvisor.dev/gvisor/pkg/state.Load"

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
	dir := os.Args[1]
	cpu := map[string]*counter{}
	var byChild []map[string]int64
	var ticks []int64
	unknownLeaf := map[string]int64{}
	for i := 1; i <= 192; i++ {
		p := read(filepath.Join(dir, fmt.Sprintf("cpu-%d", i)))
		if p.PeriodType == nil || p.PeriodType.Unit != "nanoseconds" || p.Period <= 0 {
			panic("CPU sampling period")
		}
		ticks = append(ticks, p.Period)
		totals := map[string]int64{}
		n := values(p, "samples")
		for _, s := range p.Sample {
			leaf, stk := stack(s)
			class := "outside-label"
			if labels := s.Label["restore-scope"]; len(labels) > 0 {
				if len(labels) != 1 || (labels[0] != "cold-kernel" && labels[0] != "warm-kernel") {
					panic("unexpected scope")
				}
				class = labels[0] + "/without-Load-frame"
				if stk[load] {
					class = labels[0] + "/with-Load-frame"
				}
			}
			val := s.Value[n]
			if val < 0 {
				panic("negative CPU sample")
			}
			if leaf == "[unmapped]" {
				unknownLeaf[class] += val
			}
			totals[class] += val
			update(cpu, class, val, leaf, stk)
		}
		byChild = append(byChild, totals)
	}
	alloc := map[string]*counter{}
	allocChildren := map[string][]map[string]int64{}
	for _, mode := range []string{"alloc", "blank"} {
		for i := 1; i <= 8; i++ {
			totals := map[string]int64{}
			for _, scope := range []string{"cold-kernel", "warm-kernel"} {
				for _, phase := range []string{"before", "after"} {
					p := read(filepath.Join(dir, fmt.Sprintf("%s-%d-%s-%s", mode, i, scope, phase)))
					for _, sample := range p.Sample {
						leaf, stk := stack(sample)
						cls := "without-Load-frame"
						inFixture := false
						for fn := range stk {
							if strings.Contains(fn, "/test/benchmarks/checkpointfile") && strings.Contains(fn, ".BenchmarkRestorePhases") {
								inFixture = true
							}
						}
						if stk[load] {
							cls = "Load-other"
							if inFixture && stk["runtime/pprof.Do"] {
								cls = "kernel-Load"
							}
						}
						for _, kind := range []string{"alloc_space", "alloc_objects"} {
							key := mode + "/" + scope + "/" + cls + "/" + kind
							val := sample.Value[values(p, kind)]
							if phase == "before" {
								val = -val
							}
							totals[key] += val
							update(alloc, key, val, leaf, stk)
						}
					}
				}
			}
			allocChildren[mode] = append(allocChildren[mode], totals)
		}
	}
	out := struct {
		CPU                map[string]summary
		CPUChildren        []map[string]int64
		CPUPeriodNS        []int64
		CPUUnmappedLeaf    map[string]int64
		Allocation         map[string]summary
		AllocationChildren map[string][]map[string]int64
	}{compact(cpu), byChild, ticks, unknownLeaf, compact(alloc), allocChildren}
	data, err := json.Marshal(out)
	if err != nil {
		panic(err)
	}
	fmt.Printf("profile-study %s\n", data)
}
