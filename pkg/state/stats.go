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
	"sync"
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

// Fork-only observation for a fresh-process, two-restore diagnostic. The
// repeated reader exclusively owns four measurement windows. Accesses count
// type records, not fields, objects, time, or bytes.
var typeUse struct {
	sync.Mutex
	active atomic.Int32
	phase  string
	record map[*typeInfo]*typeUseRecord
	slots  [4]int
}

type typeUseRecord struct {
	stage      int
	phase      string
	transitive bool
	uses       [4]uint16
}

const (
	useWireDeclared uint16 = 1 << iota
	useWireReference
	useLookup
	useSlotPointer
	useSlotValue
	useElement
	useKey
	useArrayFlags
	usePlainField
	useDecodeValue
)

const useConstruction = useArrayFlags | usePlainField

func (*Stats) StartTypeUseStudy(stage int) {
	typeUse.Lock()
	defer typeUse.Unlock()
	if typeUse.active.Load() != 0 || stage < 1 || stage > 4 {
		panic("overlapping or unexpected type-use measurement")
	}
	if stage == 1 {
		typeUse.record = make(map[*typeInfo]*typeUseRecord)
		typeUse.slots = [4]int{}
	}
	typeUse.phase = "setup"
	typeUse.active.Store(int32(stage))
}

func studyPhase(phase string) {
	if typeUse.active.Load() == 0 {
		return
	}
	typeUse.Lock()
	typeUse.phase = phase
	typeUse.Unlock()
}

func typeUseRecordFor(t *typeInfo) *typeUseRecord {
	r := typeUse.record[t]
	if r == nil {
		r = new(typeUseRecord)
		typeUse.record[t] = r
	}
	return r
}

func studyUse(t *typeInfo, flags uint16) *typeInfo {
	if t != nil && typeUse.active.Load() != 0 {
		typeUse.Lock()
		if stage := typeUse.active.Load(); stage != 0 {
			typeUseRecordFor(t).uses[stage-1] |= flags
		}
		typeUse.Unlock()
	}
	return t
}

func studyBuilt(newTypes []*typeInfo, root *typeInfo) {
	if typeUse.active.Load() == 0 {
		return
	}
	typeUse.Lock()
	defer typeUse.Unlock()
	stage := int(typeUse.active.Load())
	if stage == 0 {
		return
	}
	for _, t := range newTypes {
		r := typeUseRecordFor(t)
		r.stage, r.phase, r.transitive = stage, typeUse.phase, t != root
	}
}

func studyWireSlots(n int) {
	if typeUse.active.Load() == 0 {
		return
	}
	typeUse.Lock()
	if stage := typeUse.active.Load(); stage != 0 {
		typeUse.slots[stage-1] = n
	}
	typeUse.Unlock()
}

func useClass(mask uint16) string {
	switch {
	case mask&(useWireDeclared|useWireReference) != 0:
		return "wire"
	case mask&^(useConstruction|useSlotPointer) != 0:
		return "other_reached"
	case mask&useSlotPointer != 0:
		return "slot_wrapper"
	case mask&useConstruction != 0:
		return "construction_only"
	default:
		return "linked_only"
	}
}

// FinishTypeUseStudy returns counts only. Each born type appears in one
// category for its construction phase; flags also retain overlapping uses.
func (*Stats) FinishTypeUseStudy() map[string]int {
	typeUse.Lock()
	defer typeUse.Unlock()
	stage := int(typeUse.active.Swap(0))
	if stage < 1 || stage > 4 {
		panic("no active type-use measurement")
	}
	out := map[string]int{"stage": stage, "wire_slots": typeUse.slots[stage-1]}
	for _, r := range typeUse.record {
		if mask := r.uses[stage-1]; mask != 0 {
			out[fmt.Sprintf("reached_mask/%03x", mask)]++
		}
		if r.stage == 0 {
			continue
		}
		var mask uint16
		for _, u := range r.uses[:stage] {
			mask |= u
		}
		from := "root"
		if r.transitive {
			from = "nested"
		}
		prefix := fmt.Sprintf("born/%d/%s/%s/", r.stage, r.phase, from)
		out[prefix+"total"]++
		out[prefix+useClass(mask)]++
		out[fmt.Sprintf("born_mask/%d/%s/%s/%03x", r.stage, r.phase, from, mask)]++
	}
	return out
}
