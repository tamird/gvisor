// Copyright 2018 The gVisor Authors.
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

package state

import (
	"bytes"
	"fmt"
	"sort"
	"sync/atomic"
	"time"

	"gvisor.dev/gvisor/pkg/gohacks"
)

// collectStats is set by CollectStats.
var collectStats atomic.Bool

// CollectStats enables or disables the collection of per-type timing
// statistics by Save and Load, which is costly.
func CollectStats(enable bool) {
	collectStats.Store(enable)
}

type statEntry struct {
	count uint
	total time.Duration
}

// Stats describes a Save or Load.
//
// Per-type timing is only collected if enabled by CollectStats.
type Stats struct {
	// Diagnostic only. This code is absent from the independently built control.
	restorePhases [8][4]int64
	restoreShape  [3]int
	restoreMark   [4]int64
	// enabled is true if per-type timing is collected.
	enabled bool

	// objects is the number of objects.
	objects int
	// bytes is the length of the encoded object graph.
	bytes int
	// elapsed is the duration of the operation, in nanoseconds.
	elapsed int64

	// byType contains a breakdown of time spent by type.
	byType map[*typeInfo]*statEntry

	// stack contains types in progress. nil is the default entry.
	stack []*typeInfo

	// last is the monotonic time of the last sample.
	last int64
}

// init initializes statistics.
func (s *Stats) init() {
	s.byType = make(map[*typeInfo]*statEntry)
	s.last = gohacks.Nanotime()
	s.stack = append(s.stack, nil)
}

// fini finalizes statistics.
func (s *Stats) fini() {
	s.done()
}

// sample adds the time since the last sample to ti.
func (s *Stats) sample(ti *typeInfo) {
	now := gohacks.Nanotime()
	e, ok := s.byType[ti]
	if !ok {
		e = &statEntry{}
		s.byType[ti] = e
	}
	e.total += time.Duration(now - s.last)
	s.last = now
}

// start starts a sample.
func (s *Stats) start(ti *typeInfo) {
	s.sample(s.stack[len(s.stack)-1])
	s.stack = append(s.stack, ti)
}

// done finishes the current sample.
func (s *Stats) done() {
	ti := s.stack[len(s.stack)-1]
	s.sample(ti)
	s.byType[ti].count++
	s.stack = s.stack[:len(s.stack)-1]
}

// String returns a description of the stats, with a table of per-type timing
// if it was collected.
func (s *Stats) String() string {
	summary := fmt.Sprintf("%d objects, %d bytes, %v", s.objects, s.bytes, time.Duration(s.elapsed))
	if len(s.byType) == 0 {
		return summary
	}
	type entry struct {
		name  string
		entry *statEntry
	}
	ss := make([]entry, 0, len(s.byType))
	for ti, e := range s.byType {
		name := "state.default"
		if ti != nil {
			name = ti.typ.String()
		}
		ss = append(ss, entry{name, e})
	}
	sort.Slice(ss, func(i, j int) bool {
		return ss[i].entry.total > ss[j].entry.total
	})
	var (
		buf   bytes.Buffer
		count uint
		total time.Duration
	)
	buf.WriteString(summary)
	buf.WriteString("\n")
	fmt.Fprintf(&buf, "% 16s | % 8s | % 16s | %s\n", "total", "count", "per", "type")
	buf.WriteString("-----------------+----------+------------------+----------------\n")
	for _, se := range ss {
		if se.entry.count == 0 {
			continue
		}
		count += se.entry.count
		total += se.entry.total
		per := se.entry.total / time.Duration(se.entry.count)
		fmt.Fprintf(&buf, "% 16s | %8d | % 16s | %s\n",
			se.entry.total, se.entry.count, per, se.name)
	}
	buf.WriteString("-----------------+----------+------------------+----------------\n")
	if count > 0 {
		fmt.Fprintf(&buf, "% 16s | % 8d | % 16s | [all]", total, count, total/time.Duration(count))
	}
	return buf.String()
}

// RestorePhaseSample returns the phase samples only after state.Load returns.
func (s *Stats) RestorePhaseSample() ([8][4]int64, [3]int) {
	return s.restorePhases, s.restoreShape
}

var restoreMissCalls, restoreBuiltTypes, restoreMissTime atomic.Int64

func phaseMark() [4]int64 {
	return [4]int64{gohacks.Nanotime(), restoreMissCalls.Load(), restoreBuiltTypes.Load(), restoreMissTime.Load()}
}

func (s *Stats) endRestorePhase(i int) {
	mark := phaseMark()
	for n, v := range mark {
		s.restorePhases[i][n] = v - s.restoreMark[n]
	}
	s.restoreMark = mark
}
