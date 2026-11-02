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

package state

import (
	"bytes"
	"container/heap"
	"context"
	"fmt"
	"io"
	"reflect"
	"sort"
	"unsafe"

	"gvisor.dev/gvisor/pkg/gohacks"
)

// load decodes the stream in data into the value of type ti at root.
func (ds *graphDecodeState) load(root unsafe.Pointer, ti *typeInfo) {
	d := &ds.in
	if magic := d.uint32(); magic != streamMagic {
		Failf("invalid stream: bad magic %#x", magic)
	}
	if f := d.uint64(); f != schemaFingerprint() {
		Failf("the state was saved by a binary with different types (fingerprint %#x, want %#x)", f, schemaFingerprint())
	}
	ds.readTypes()
	ds.readObjects(root, ti)
	ds.readTargets()
	ds.decodeObjects()
	setHints(ti, len(d.b), len(ds.targets))
	if d.pos != len(d.b) {
		Failf("%d bytes of trailing data", len(d.b)-d.pos)
	}
	ds.runCallbacks()
}

// decodeObjects decodes the data of all objects.
func (ds *graphDecodeState) decodeObjects() {
	d := &ds.in
	ds.graphLen = len(d.b)
	var deferred []deferredObject
	for i := range ds.objs {
		ds.cur = int32(i)
		o := &ds.objs[i]
		if o.kind != objKindMap && !o.inexact {
			ds.decodeObject(int32(i))
			continue
		}
		n := d.byteLength()
		end := d.pos + n
		if o.decodeType == nil {
			// Decode the object once it has been referred to.
			deferred = append(deferred, deferredObject{obj: int32(i), pos: d.pos, end: end})
			d.pos = end
			continue
		}
		ds.decodeFrame(int32(i), end)
	}
	if len(deferred) != 0 {
		ds.decodeDeferred(deferred)
	}
}

// decodeFrame confines the object to the bytes its length prefix declares.
func (ds *graphDecodeState) decodeFrame(obj int32, end int) {
	d := &ds.in
	stream := d.b
	d.b = stream[:end]
	ds.decodeObject(obj)
	if d.pos != end {
		Failf("object %d has %d bytes of trailing data", obj, end-d.pos)
	}
	d.b = stream
}

// decodeObject decodes the data of the object obj.
func (ds *graphDecodeState) decodeObject(obj int32) {
	d := &ds.in
	o := &ds.objs[obj]
	ti := o.decodeType
	switch o.kind {
	case objKindValue:
		ds.keepZero = obj == 0
		ds.decodeValue(ti, o.base)
		ds.keepZero = false
	case objKindArray:
		n := d.count(1)
		esz := ti.size
		var pos uint64
		for j := 0; j < n; j++ {
			pos += d.uvarint()
			count := d.uvarint()
			if pos+count > uint64(o.size/esz) || pos+count < pos {
				Failf("invalid chunk %d+%d of %d %v", pos, count, o.size/esz, ti.typ)
			}
			ds.decodeElems(ti, add(o.base, uintptr(pos)*esz), uintptr(count))
			pos += count
		}
	case objKindMap:
		ds.decodeMap(ti, o.base, int(o.size))
	default:
		Failf("unexpected object kind %d", o.kind)
	}
}

// deferredObject records a skipped payload and the first pass that can decode it.
type deferredObject struct {
	obj      int32
	pass     uint32
	pos, end int
}

type readyObjects []*deferredObject

func (q readyObjects) Len() int      { return len(q) }
func (q readyObjects) Swap(i, j int) { q[i], q[j] = q[j], q[i] }
func (q readyObjects) Less(i, j int) bool {
	if q[i].pass == q[j].pass {
		return q[i].obj < q[j].obj
	}
	return q[i].pass < q[j].pass
}
func (q *readyObjects) Push(v any) { *q = append(*q, v.(*deferredObject)) }
func (q *readyObjects) Pop() any {
	last := len(*q) - 1
	v := (*q)[last]
	*q = (*q)[:last]
	return v
}

type deferredDecode struct {
	rows  []deferredObject // In wire order.
	ready readyObjects
	pass  uint32
}

// typeKnown is called the first time a reference supplies an object's type.
func (ds *graphDecodeState) typeKnown(obj int32) {
	if ds.pending != nil {
		ds.pending.wake(obj, ds.cur)
	}
}

func (q *deferredDecode) wake(obj, cur int32) {
	i := sort.Search(len(q.rows), func(i int) bool { return q.rows[i].obj >= obj })
	if i == len(q.rows) || q.rows[i].obj != obj {
		return
	}
	row := &q.rows[i]
	row.pass = q.pass
	// A wire-order scan would already have passed an earlier row. Preserve
	// the order of StateLoad calls and the first error in a malformed stream.
	if obj < cur {
		row.pass++
	}
	heap.Push(&q.ready, row)
}

func (ds *graphDecodeState) decodeDeferred(rows []deferredObject) {
	q := &deferredDecode{rows: rows}
	ds.pending = q
	for i := range rows {
		if ds.objs[rows[i].obj].decodeType != nil {
			// The initial entries are already in heap order.
			q.ready = append(q.ready, &rows[i])
		}
	}
	end := ds.in.pos
	for range rows {
		if len(q.ready) == 0 {
			for _, row := range rows {
				if ds.objs[row.obj].decodeType == nil {
					Failf("the type of object %d (%s) is not known from references to it", row.obj, ds.objTypeName(row.obj))
				}
			}
		}
		row := heap.Pop(&q.ready).(*deferredObject)
		q.pass = row.pass
		ds.cur = row.obj
		ds.in.pos = row.pos
		ds.decodeFrame(row.obj, row.end)
	}
	ds.in.pos = end
	ds.pending = nil
}

// loadValue decodes a value of type ti for Source.LoadValueTo, and calls
// l.StateLoadValue with it once the objects it refers to are complete.
func (ds *graphDecodeState) loadValue(ti *typeInfo, slot int, l ValueLoader) {
	// Values are decoded to storage that is reused while the values are
	// only passed to l immediately: generated StateLoadValue methods copy
	// them. Values passed later, or whose decoding ran code that may keep
	// their address, keep their storage.
	p := ds.valueScratch[ti]
	if p == nil {
		p = reflect.New(ti.typ).UnsafePointer()
	} else {
		delete(ds.valueScratch, ti)
		if ti.partial {
			reflect.NewAt(ti.typ, p).Elem().SetZero()
		}
	}
	hooks, deferred, refs := ds.hooks, ds.deferred, ds.refs
	ds.lastRef = -1
	keepZero := ds.keepZero
	ds.keepZero = false
	ds.decodeValue(ti, p)
	ds.keepZero = keepZero
	if ds.deferred != deferred {
		if ds.lastRef >= 0 && ds.lastRef != ds.cur && ds.lastRef != 0 {
			ds.wait(ds.cur, ds.lastRef, decodeCallback{waiter: -1})
		}
		if !ds.noCallbacks {
			valPtr := p
			ds.addCallback(ds.cur, decodeCallback{fn: func() { l.StateLoadValue(ds.ctx, slot, makeAny(ti, valPtr)) }, waiter: -1})
		}
		return
	}
	cb := decodeCallback{valueLoader: l, slot: slot, value: makeAny(ti, p), waiter: -1}
	if ds.lastRef >= 0 {
		if ds.lastRef != ds.cur && ds.lastRef != 0 {
			ds.wait(ds.cur, ds.lastRef, cb)
			return
		}
	} else if ds.refs != refs {
		if !ds.noCallbacks {
			ds.addCallback(ds.cur, cb)
		}
		return
	}
	reusable := ds.hooks == hooks
	ds.runCallback(&cb)
	if reusable {
		if ds.valueScratch == nil {
			ds.valueScratch = make(map[*typeInfo]unsafe.Pointer)
		}
		ds.valueScratch[ti] = p
	}
}

// runCallbacks runs the callbacks of objects as they become complete.
func (ds *graphDecodeState) runCallbacks() {
	leaves := ds.leaves[:0]
	defer func() { ds.leaves = leaves }()
	for i := range ds.ods {
		o := &ds.ods[i]
		if o.waitHead >= 0 {
			if o.cbTail < 0 {
				o.cbHead = o.waitHead
			} else {
				ds.cbs[o.cbTail].next = o.waitHead
			}
			o.cbTail = o.waitTail
		}
		if o.blockedBy == 0 {
			leaves = append(leaves, int32(i))
		}
	}
	pending := len(ds.ods)
	var cur int32
	if err := safely(func() {
		for i := 0; i < len(leaves); i++ {
			cur = leaves[i]
			o := &ds.ods[cur]
			if o.blockedBy != 0 || o.complete {
				continue
			}
			if ds.stats.enabled && o.cbHead >= 0 && ds.objs[cur].ti != nil {
				ds.stats.start(ds.objs[cur].ti)
			}
			for c := o.cbHead; c >= 0; c = ds.cbs[c].next {
				cb := &ds.cbs[c]
				if cb.waiter >= 0 {
					w := &ds.ods[cb.waiter]
					w.blockedBy--
					if w.blockedBy == 0 {
						leaves = append(leaves, cb.waiter)
					} else if w.blockedBy < 0 {
						Failf("object %d has negative blockedBy: %d", cb.waiter, w.blockedBy)
					}
					continue
				}
				ds.runCallback(cb)
			}
			if ds.stats.enabled && o.cbHead >= 0 && ds.objs[cur].ti != nil {
				ds.stats.done()
			}
			o = &ds.ods[cur]
			o.complete = true
			pending--
		}
	}); err != nil {
		Failf("error executing callbacks: %w\nfor object of type %v", err, ds.objTypeName(cur))
	}
	if pending > 0 {
		cycle := ds.findCycle()
		var buf bytes.Buffer
		buf.WriteString("dependency cycle: {")
		for i, obj := range cycle {
			if i > 0 {
				buf.WriteString(" => ")
			}
			fmt.Fprintf(&buf, "%q", ds.objTypeName(obj))
		}
		buf.WriteString("}")
		Failf("incomplete graph: %s", buf.String())
	}
}

func (ds *graphDecodeState) objTypeName(obj int32) string {
	o := &ds.objs[obj]
	ti := o.decodeType
	if ti == nil {
		ti = o.ti
	}
	switch o.kind {
	case objKindValue:
		if ti == nil {
			return "unknown value"
		}
		if o.inexact && o.decodeType == nil {
			return fmt.Sprintf("%v (inexact layout)", ti.typ)
		}
		return ti.typ.String()
	case objKindArray:
		if ti == nil || ti.size == 0 {
			return "array"
		}
		if o.inexact && o.decodeType == nil {
			return fmt.Sprintf("[%d]%v (inexact layout)", o.size/ti.size, ti.typ)
		}
		return fmt.Sprintf("[%d]%v", o.size/ti.size, ti.typ)
	case objKindMap:
		if o.decodeType != nil {
			return o.decodeType.typ.String()
		}
		return fmt.Sprintf("map (%d entries)", o.size)
	default:
		return fmt.Sprintf("unknown object kind %d", o.kind)
	}
}

// findCycle finds a dependency cycle after all unblocked objects have
// completed. Every remaining object has a blocker that is also incomplete.
// Following one blocker per object must reach a cycle, even if the starting
// object is only waiting on that cycle.
func (ds *graphDecodeState) findCycle() []int32 {
	blockers := make(map[int32]int32)
	first := int32(-1)
	for i := range ds.ods {
		o := &ds.ods[i]
		if o.complete {
			continue
		}
		if first < 0 {
			first = int32(i)
		}
		for c := o.cbHead; c >= 0; c = ds.cbs[c].next {
			if w := ds.cbs[c].waiter; w >= 0 {
				blockers[w] = int32(i)
			}
		}
	}
	visited := make(map[int32]int)
	var path []int32
	for obj, ok := first, true; ok; obj, ok = blockers[obj] {
		if start, ok := visited[obj]; ok {
			return append(path[start:], obj)
		}
		visited[obj] = len(path)
		path = append(path, obj)
	}
	Failf("incomplete object has no blocking dependency")
	panic("unreachable")
}

func (gd *graphStructDecoder) load(slot int, objPtr any) {
	e := efaceOf(&objPtr)
	ti := gd.st.slotType(slot, e.typ, true)
	gd.ds.decodeValue(ti, e.data)
}

func (gd *graphStructDecoder) loadWait(slot int, objPtr any) {
	e := efaceOf(&objPtr)
	ti := gd.st.slotType(slot, e.typ, true)
	ds := gd.ds
	ds.lastRef = -1
	ds.decodeValue(ti, e.data)
	if ds.lastRef >= 0 {
		ds.wait(ds.cur, ds.lastRef, decodeCallback{waiter: -1})
	}
}

func deferLoadValueCallback(objPtr any, fn func(any)) decodeCallback {
	return decodeCallback{
		fn:     func() { fn(reflect.ValueOf(objPtr).Elem().Interface()) },
		waiter: -1,
	}
}

func (gd *graphStructDecoder) loadValue(slot int, objPtr any, fn func(any)) {
	e := efaceOf(&objPtr)
	ti := gd.st.slotType(slot, e.typ, true)
	ds := gd.ds
	deferred, refs := ds.deferred, ds.refs
	ds.lastRef = -1
	keepZero := ds.keepZero
	ds.keepZero = false
	ds.decodeValue(ti, e.data)
	ds.keepZero = keepZero
	if ds.deferred != deferred {
		if ds.lastRef >= 0 && ds.lastRef != ds.cur && ds.lastRef != 0 {
			ds.wait(ds.cur, ds.lastRef, decodeCallback{waiter: -1})
		}
		if !ds.noCallbacks {
			ds.addCallback(ds.cur, deferLoadValueCallback(objPtr, fn))
		}
		return
	}
	if ds.lastRef >= 0 {
		if ds.lastRef != ds.cur && ds.lastRef != 0 {
			ds.wait(ds.cur, ds.lastRef, deferLoadValueCallback(objPtr, fn))
			return
		}
	} else if ds.refs != refs {
		if !ds.noCallbacks {
			ds.addCallback(ds.cur, deferLoadValueCallback(objPtr, fn))
		}
		return
	}
	if !ds.noCallbacks {
		ds.hooks++
		fn(reflect.ValueOf(objPtr).Elem().Interface())
	}
}

func (gd *graphStructDecoder) loadValueTo(slot int, typ any, l ValueLoader) {
	e := efaceOf(&typ)
	ti := gd.st.slotType(slot, e.typ, true)
	gd.ds.loadValue(ti, slot, l)
}

func (gd *graphStructDecoder) afterLoad(fn func()) {
	ds := gd.ds
	if !ds.noCallbacks {
		ds.addCallback(ds.cur, decodeCallback{fn: fn, waiter: -1})
	}
}

func (gd *graphStructDecoder) afterLoadTo(l AfterLoader) {
	ds := gd.ds
	if !ds.noCallbacks {
		ds.addCallback(ds.cur, decodeCallback{afterLoader: l, waiter: -1})
	}
}

func loadGraph(ctx context.Context, r io.Reader, rootPtr any, length uint64, start int64) (Stats, error) {
	ds := newGraphDecodeState(ctx)
	defer ds.release()
	ds.stats.used = true
	ds.stats.enabled = collectStats.Load()
	if length > maxStreamLength {
		return Stats{graph: ds.stats}, fmt.Errorf("invalid object graph length %d", length)
	}
	data := make([]byte, length)
	if _, err := io.ReadFull(r, data); err != nil {
		return Stats{graph: ds.stats}, err
	}
	ds.in = decBuf{b: data}
	err := safely(func() {
		e := efaceOf(&rootPtr)
		if e.typ == nil {
			Failf("Load requires a non-nil pointer, got %T", rootPtr)
		}
		ti := typeInfoForRtype(e.typ)
		if ti.kind != reflect.Pointer || e.data == nil {
			Failf("Load requires a non-nil pointer, got %T", rootPtr)
		}
		if ds.stats.enabled {
			ds.stats.init()
			defer ds.stats.done()
		}
		ds.load(e.data, ti.elem)
	})
	ds.stats.bytes = len(data)
	ds.stats.objects = len(ds.objs)
	ds.stats.elapsed = gohacks.Nanotime() - start
	return Stats{graph: ds.stats}, err
}
