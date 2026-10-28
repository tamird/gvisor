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
	"cmp"
	"context"
	"reflect"
	"slices"
	"sync"
	"unsafe"
)

// objectState is the encoding state of an object.
type objectState uint8

const (
	// objQueued objects are waiting to be encoded.
	objQueued objectState = iota
	// objEncoding objects are being encoded.
	objEncoding
	// objDone objects have been encoded.
	objDone
)

// segment is a range of encodeState.out.
type segment struct {
	off, len int
}

// elemRange is a range of array elements [start, end).
type elemRange struct {
	start, end uintptr
}

// chunk is the encoding of a range of array elements.
type chunk struct {
	elemRange
	seg segment
}

// encObject is an object: a region of memory saved as a unit, which pointers
// may point into.
//
// Objects are either single values, arrays (the backing arrays of slices, of
// which only the elements within slices or pointed to are saved) or maps
// (which are referred to by their map pointer).
//
// Live objects never overlap. When an object is found that contains other
// objects, it absorbs them: their targets are relocated into it, and their
// encodings, if done, are reused so that StateSave is called once per value.
type encObject struct {
	// base is the address of the object, and keeps it alive.
	base      unsafe.Pointer
	addr, end uintptr
	ti        *typeInfo
	n         uintptr
	isArray   bool
	isMap     bool
	state     objectState

	// parent is the object that absorbed this object, or -1.
	parent    int32
	parentOff uintptr

	// seg is the encoding of single values and maps.
	seg segment
	// cov and ch are the covered elements and the encoded chunk of arrays
	// with a single range of each. Further ranges are in x.
	cov    elemRange
	ch     chunk
	hasCov bool
	hasCh  bool

	// x holds the state of arrays and of objects that absorbed others.
	x *objExtra

	// stamp deduplicates query results.
	stamp uint32
	// final is the index of the object in the stream.
	final int32
}

// objExtra is the state of arrays and of objects that absorbed others.
type objExtra struct {
	// children are the objects absorbed by this object.
	children []int32
	// covered are the elements of arrays that are encoded or queued, sorted.
	covered []elemRange
	// chunks are the encoded elements of arrays.
	chunks []chunk
}

func (o *encObject) extra() *objExtra {
	if o.x == nil {
		o.x = &objExtra{}
	}
	return o.x
}

func (o *encObject) childList() []int32 {
	if o.x == nil {
		return nil
	}
	return o.x.children
}

// coveredList returns the covered elements of the array o. The result is only
// valid until the next call.
func (es *encodeState) coveredList(o *encObject) []elemRange {
	if o.hasCov {
		es.tmpRange[0] = o.cov
		return es.tmpRange[:]
	}
	if o.x == nil {
		return nil
	}
	return o.x.covered
}

// setCovered sets the covered elements of the array o.
func (es *encodeState) setCovered(o *encObject, rs []elemRange) {
	if len(rs) == 1 && (o.x == nil || o.x.covered == nil) {
		o.cov, o.hasCov = rs[0], true
		return
	}
	o.hasCov = false
	x := o.extra()
	x.covered = append(x.covered[:0], rs...)
}

// chunkList returns the encoded chunks of the array o. The result is only
// valid until the next call.
func (es *encodeState) chunkList(o *encObject) []chunk {
	if o.hasCh {
		es.tmpChunk[0] = o.ch
		return es.tmpChunk[:]
	}
	if o.x == nil {
		return nil
	}
	return o.x.chunks
}

// addChunk adds an encoded chunk of the array o.
func (es *encodeState) addChunk(o *encObject, ch chunk) {
	if !o.hasCh && (o.x == nil || o.x.chunks == nil) {
		o.ch, o.hasCh = ch, true
		return
	}
	x := o.extra()
	if o.hasCh {
		x.chunks = append(x.chunks, o.ch)
		o.hasCh = false
	}
	x.chunks = append(x.chunks, ch)
}

// encTarget is the target of pointers: a location within an object.
type encTarget struct {
	obj int32
	off uintptr
	ti  *typeInfo
	// next is the next target with the same address, or -1.
	next int32
	// addr is the address of zero-sized targets, which are resolved after
	// all objects are known.
	addr uintptr
}

// work is an object, or a range of array elements, to encode.
type work struct {
	obj int32
	elemRange
}

// blockShift is the log2 of the granularity of the object index.
const blockShift = 9

// blockEntry is an entry in a list of objects intersecting a block.
type blockEntry struct {
	// addr and end are the range of the object, so that entries can be
	// checked without accessing the object.
	addr, end uintptr
	obj, next int32
}

// splice is an encoding to reuse when encoding a containing object.
type splice struct {
	addr  uintptr
	ti    *typeInfo
	count uintptr
	seg   segment
}

// traversalKey identifies a containment check.
type traversalKey struct {
	root, target *typeInfo
	off          uintptr
	count        uintptr
}

// encodeState is the state of Save.
type encodeState struct {
	ctx context.Context

	// out holds the encodings of objects.
	out encBuf

	// final holds the stream.
	final encBuf

	objs    chunked[encObject]
	targets chunked[encTarget]

	typeCache typeCache

	// byAddr maps addresses to the first target at that address.
	byAddr addrMap
	// maps maps map pointers to their targets.
	maps map[unsafe.Pointer]int32
	// blocks maps blocks of memory to the objects that intersect them.
	blocks       addrMap
	blockEntries []blockEntry
	stamp        uint32
	queryResult  []int32

	queue     []work
	queueHead int

	// zeroTargets are targets of zero-sized types.
	zeroTargets []int32

	traversals map[traversalKey]bool

	// splices are the encodings to reuse while encoding the current work,
	// sorted by address. spliceAt is the address of the next splice.
	splices   []splice
	spliceIdx int
	spliceAt  uintptr

	types typeTable

	binaryBuf []byte

	mapSlots map[*typeInfo]mapSlots

	// coverBuf, absorbBuf, tmpRange and tmpChunk are scratch storage.
	coverBuf  []elemRange
	absorbBuf []int32
	tmpRange  [1]elemRange
	tmpChunk  [1]chunk

	// mapPtr holds the map being encoded.
	mapPtr unsafe.Pointer

	// copies are storage for copies of values, by type.
	copies map[*typeInfo][]reflect.Value

	stats Stats
}

// dummyAddr is the address of zero-sized allocations.
var dummyAddr = reflect.ValueOf(new(struct{})).Pointer()

const noSplice = ^uintptr(0)

// encodeStatePool holds encodeStates for reuse, which avoids growing their
// buffers and maps for each Save.
var encodeStatePool sync.Pool

// maxPooledBytes limits the buffer capacity of pooled states.
const maxPooledBytes = 16 << 20

func newEncodeState(ctx context.Context, root *typeInfo) *encodeState {
	if es, ok := encodeStatePool.Get().(*encodeState); ok {
		es.ctx = ctx
		return es
	}
	// Graphs with the same root type tend to have similar sizes, so start
	// with the size of the last such graph saved or loaded.
	bytes, targets := int(root.hintBytes.Load()), int(root.hintTargets.Load())
	es := &encodeState{
		ctx:          ctx,
		maps:         make(map[unsafe.Pointer]int32),
		blockEntries: make([]blockEntry, 0, targets),
		queue:        make([]work, 0, targets),
		traversals:   make(map[traversalKey]bool),
		spliceAt:     noSplice,
		out:          encBuf{b: make([]byte, 0, max(bytes, 4<<10))},
	}
	es.byAddr.init(targets)
	es.blocks.init(targets)
	return es
}

// setHints records the length and number of targets of a graph with the
// given root type, capped to limit the effect of outliers.
func setHints(root *typeInfo, bytes, targets int) {
	root.hintBytes.Store(int32(min(bytes, maxPooledBytes/2)))
	root.hintTargets.Store(int32(min(targets, maxPooledBytes/64)))
}

// chunkedShift is the log2 of the number of elements per chunk of chunked.
const chunkedShift = 10

// chunked is an append-only array that is not copied as it grows.
type chunked[T any] struct {
	chunks [][]T
	n      int
}

// add appends a zero element and returns a pointer to it. Pointers to the
// first chunk's elements are invalidated as it grows.
func (c *chunked[T]) add() *T {
	i := c.n
	c.n++
	if i < 1<<chunkedShift {
		// The first chunk grows as needed, for small graphs.
		if len(c.chunks) == 0 {
			c.chunks = append(c.chunks, nil)
		}
		var zero T
		c.chunks[0] = append(c.chunks[0], zero)
		return &c.chunks[0][i]
	}
	if i>>chunkedShift == len(c.chunks) {
		c.chunks = append(c.chunks, make([]T, 1<<chunkedShift))
	}
	return &c.chunks[i>>chunkedShift][i&(1<<chunkedShift-1)]
}

// at returns a pointer to element i.
func (c *chunked[T]) at(i int) *T {
	return &c.chunks[i>>chunkedShift][i&(1<<chunkedShift-1)]
}

func (es *encodeState) obj(i int32) *encObject {
	return es.objs.at(int(i))
}

func (es *encodeState) target(i int32) *encTarget {
	return es.targets.at(int(i))
}

// reset clears all elements.
func (c *chunked[T]) reset() {
	for i := range c.chunks {
		clear(c.chunks[i][:cap(c.chunks[i])])
	}
	if len(c.chunks) > 0 {
		c.chunks[0] = c.chunks[0][:0]
	}
	c.n = 0
}

// release returns es to encodeStatePool, if it is not too large.
func (es *encodeState) release() {
	if cap(es.out.b)+cap(es.final.b) > maxPooledBytes {
		return
	}
	es.ctx = nil
	es.out.b = es.out.b[:0]
	es.final.b = es.final.b[:0]
	es.objs.reset()
	es.targets.reset()
	es.byAddr.reset()
	clear(es.maps)
	es.blocks.reset()
	es.blockEntries = es.blockEntries[:0]
	es.queryResult = es.queryResult[:0]
	es.queue = es.queue[:0]
	es.queueHead = 0
	es.zeroTargets = es.zeroTargets[:0]
	es.splices = es.splices[:0]
	es.spliceIdx = 0
	es.spliceAt = noSplice
	es.types.reset()
	for _, sc := range es.mapSlots {
		sc.k.SetZero()
		sc.e.SetZero()
	}
	es.mapPtr = nil
	es.stats = Stats{}
	encodeStatePool.Put(es)
}

// newObject adds an object, without indexing it.
func (es *encodeState) newObject(base unsafe.Pointer, end uintptr, ti *typeInfo, n uintptr, isArray bool) int32 {
	o := es.objs.add()
	*o = encObject{
		base:    base,
		addr:    uintptr(base),
		end:     end,
		ti:      ti,
		n:       n,
		isArray: isArray,
		parent:  -1,
	}
	return int32(es.objs.n - 1)
}

// index adds the object to the object index.
func (es *encodeState) index(obj int32) {
	o := es.obj(obj)
	for b := o.addr >> blockShift; b <= (o.end-1)>>blockShift; b++ {
		head, ok := es.blocks.get(b)
		if !ok {
			head = -1
		}
		es.blockEntries = append(es.blockEntries, blockEntry{addr: o.addr, end: o.end, obj: obj, next: head})
		es.blocks.set(b, int32(len(es.blockEntries)-1))
	}
}

// query returns the live objects overlapping [addr, end). The result is only
// valid until the next call.
func (es *encodeState) query(addr, end uintptr) []int32 {
	es.stamp++
	es.queryResult = es.queryResult[:0]
	for b := addr >> blockShift; b <= (end-1)>>blockShift; b++ {
		e, ok := es.blocks.get(b)
		if !ok {
			continue
		}
		for ; e >= 0; e = es.blockEntries[e].next {
			be := &es.blockEntries[e]
			if be.addr >= end || addr >= be.end {
				continue
			}
			o := es.obj(be.obj)
			if o.parent >= 0 || o.stamp == es.stamp {
				continue
			}
			o.stamp = es.stamp
			es.queryResult = append(es.queryResult, be.obj)
		}
	}
	return es.queryResult
}

// root returns the live object containing obj, and the offset of obj in it.
func (es *encodeState) root(obj int32) (int32, uintptr) {
	var off uintptr
	for es.obj(obj).parent >= 0 {
		off += es.obj(obj).parentOff
		obj = es.obj(obj).parent
	}
	return obj, off
}

// newTarget adds a target.
func (es *encodeState) newTarget(obj int32, off uintptr, ti *typeInfo) int32 {
	*es.targets.add() = encTarget{obj: obj, off: off, ti: ti, next: -1}
	return int32(es.targets.n - 1)
}

// lookup returns the target at addr with type ti, or -1.
func (es *encodeState) lookup(addr uintptr, ti *typeInfo) int32 {
	t, ok := es.byAddr.get(addr)
	if !ok {
		return -1
	}
	for ; t >= 0; t = es.target(t).next {
		if es.target(t).ti == ti {
			return t
		}
	}
	return -1
}

// addLookup makes tid findable by lookup.
func (es *encodeState) addLookup(addr uintptr, tid int32) {
	if head, ok := es.byAddr.get(addr); ok {
		es.target(tid).next = es.target(head).next
		es.target(head).next = tid
		return
	}
	es.byAddr.set(addr, tid)
}

// enqueue queues work.
func (es *encodeState) enqueue(obj int32, start, end uintptr) {
	es.queue = append(es.queue, work{obj: obj, elemRange: elemRange{start, end}})
}

// cover ensures that elements [start, end) of the live object obj are
// encoded.
func (es *encodeState) cover(obj int32, start, end uintptr) {
	o := es.obj(obj)
	if !o.isArray {
		return
	}
	end = min(end, o.n)
	if start >= end {
		return
	}
	covered := es.coveredList(o)
	// Fast path: already covered by a single range.
	for _, r := range covered {
		if r.start <= start && end <= r.end {
			return
		}
	}
	// Queue the uncovered parts, and merge the overlapping and adjacent
	// ranges into [start, end).
	pos := start
	merged := es.coverBuf[:0]
	inserted := false
	for _, r := range covered {
		if r.end < start {
			merged = append(merged, r)
			continue
		}
		if r.start > end {
			if !inserted {
				merged = append(merged, elemRange{start, end})
				inserted = true
			}
			merged = append(merged, r)
			continue
		}
		if r.start > pos {
			es.enqueue(obj, pos, r.start)
		}
		pos = max(pos, r.end)
		start = min(start, r.start)
		end = max(end, r.end)
	}
	if pos < end {
		es.enqueue(obj, pos, end)
	}
	if !inserted {
		merged = append(merged, elemRange{start, end})
	}
	es.coverBuf = merged
	es.setCovered(es.obj(obj), merged)
}

// markCovered marks elements of the live array obj as covered, without
// queueing them.
func (es *encodeState) markCovered(obj int32, rs []elemRange, shift uintptr) {
	o := es.obj(obj)
	all := append(es.coverBuf[:0], es.coveredList(o)...)
	for _, r := range rs {
		all = append(all, elemRange{r.start + shift, r.end + shift})
	}
	slices.SortFunc(all, func(a, b elemRange) int { return cmp.Compare(a.start, b.start) })
	merged := all[:0]
	for _, r := range all {
		if n := len(merged); n > 0 && r.start <= merged[n-1].end {
			merged[n-1].end = max(merged[n-1].end, r.end)
			continue
		}
		merged = append(merged, r)
	}
	es.coverBuf = merged
	es.setCovered(o, merged)
}

// contains returns true if count consecutive values of type target at offset
// off of a value of type root are a field or elements of it.
func (es *encodeState) contains(root, target *typeInfo, off, count uintptr) bool {
	k := traversalKey{root, target, off, count}
	if v, ok := es.traversals[k]; ok {
		return v
	}
	v := containsType(root, target, off, count)
	es.traversals[k] = v
	return v
}

func containsType(root, target *typeInfo, off, count uintptr) bool {
	if count <= 1 && root == target && off == 0 {
		return true
	}
	if off > root.size || target.size*count > root.size-off {
		return false
	}
	switch root.kind {
	case reflect.Struct:
		t := root.typ
		for i := t.NumField() - 1; i >= 0; i-- {
			f := t.Field(i)
			// Zero-sized fields may share an offset with other fields, so
			// keep searching on failure.
			if f.Offset <= off && containsType(typeInfoOf(f.Type), target, off-f.Offset, count) {
				return true
			}
		}
	case reflect.Array:
		if root.length == 0 {
			return false
		}
		esz := root.elem.size
		if esz == 0 {
			return containsType(root.elem, target, off, count)
		}
		if root.elem == target && off%esz == 0 && off/esz+count <= root.length {
			return true
		}
		if target.kind == reflect.Array && target.elem == root.elem && off%esz == 0 && off/esz+target.length*count <= root.length {
			return true
		}
		return count <= 1 && containsType(root.elem, target, off%esz, count)
	}
	return false
}

// checkInterior checks that count values of type ti at offset off of the
// live object obj are a part of it, and ensures that the array elements they
// are in are encoded.
func (es *encodeState) checkInterior(obj int32, off uintptr, ti *typeInfo, count uintptr) {
	o := es.obj(obj)
	if o.isMap {
		Failf("pointer into map %v", o.ti.typ)
	}
	if !o.isArray {
		if !es.contains(o.ti, ti, off, count) {
			Failf("%v at offset %d is not a field of %v", ti.typ, off, o.ti.typ)
		}
		return
	}
	esz := o.ti.size
	k, sub := off/esz, off%esz
	size := ti.size * count
	if size == 0 {
		size = 1
	}
	if sub == 0 && (ti == o.ti || (ti.kind == reflect.Array && ti.elem == o.ti)) {
		// Elements.
	} else if !es.contains(o.ti, ti, sub, count) {
		Failf("%v at offset %d is not within an array of %v", ti.typ, off, o.ti.typ)
	}
	es.cover(obj, k, (off+size-1)/esz+1)
}

// absorb makes the live object obj, which must be contained in the new object
// parent, part of parent.
func (es *encodeState) absorb(parent, obj int32) {
	p, o := es.obj(parent), es.obj(obj)
	off := o.addr - p.addr
	o.parent = parent
	o.parentOff = off
	p.extra().children = append(p.extra().children, obj)
	count := uintptr(1)
	if o.isArray {
		count = o.n
	}
	if !p.isArray {
		if !es.contains(p.ti, o.ti, off, count) {
			Failf("%v at offset %d is not a field of %v", o.ti.typ, off, p.ti.typ)
		}
		return
	}
	if esz := p.ti.size; o.isArray && o.ti == p.ti && off%esz == 0 {
		// The absorbed array's queued and encoded elements are elements of
		// parent; its queued elements are still encoded as its own.
		rs := append([]elemRange(nil), es.coveredList(o)...)
		es.markCovered(parent, rs, off/esz)
		return
	}
	es.checkInterior(parent, off, o.ti, count)
}

// containsRange returns true if the live object obj contains count values of
// type ti in [addr, end). If obj has the same range, this depends on whether
// ti is a field of obj's type, or the converse.
func (es *encodeState) containsRange(obj int32, addr, end uintptr, ti *typeInfo, count uintptr) bool {
	o := es.obj(obj)
	if o.addr > addr || end > o.end {
		return false
	}
	if o.addr < addr || end < o.end || o.isArray || o.isMap {
		return true
	}
	// Same range: the larger type in the field hierarchy is the container.
	if count == 1 && es.contains(o.ti, ti, 0, 1) {
		return true
	}
	return !es.contains(ti, o.ti, 0, 1) && !(count > 1 && ti == o.ti)
}

// newContainer adds an object that absorbs the live objects in ov.
func (es *encodeState) newContainer(base unsafe.Pointer, end uintptr, ti *typeInfo, n uintptr, isArray bool, ov []int32) int32 {
	ov = append(es.absorbBuf[:0], ov...)
	es.absorbBuf = ov
	obj := es.newObject(base, end, ti, n, isArray)
	for _, c := range ov {
		es.absorb(obj, c)
	}
	es.index(obj)
	return obj
}

// ptrTarget returns the target for a pointer to a value of type ti at addr.
func (es *encodeState) ptrTarget(p unsafe.Pointer, ti *typeInfo) int32 {
	addr := uintptr(p)
	if tid := es.lookup(addr, ti); tid >= 0 {
		return tid
	}
	var tid int32
	if ti.size == 0 {
		// Zero-sized values may share addresses with other values, so they
		// are not objects. They are resolved once all objects are known.
		tid = es.newTarget(-1, 0, ti)
		es.target(tid).addr = addr
		es.zeroTargets = append(es.zeroTargets, tid)
	} else {
		end := addr + ti.size
		ov := es.query(addr, end)
		if len(ov) == 1 && es.containsRange(ov[0], addr, end, ti, 1) {
			obj := ov[0]
			off := addr - es.obj(obj).addr
			es.checkInterior(obj, off, ti, 1)
			tid = es.newTarget(obj, off, ti)
		} else {
			for _, c := range ov {
				if co := es.obj(c); co.addr < addr || co.end > end {
					Failf("%v at %#x overlaps %v at %#x", ti.typ, addr, co.ti.typ, co.addr)
				}
			}
			obj := es.newContainer(p, end, ti, 1, false, ov)
			es.enqueue(obj, 0, 1)
			tid = es.newTarget(obj, 0, ti)
		}
	}
	es.addLookup(addr, tid)
	return tid
}

// sliceTarget returns the target for the data of a slice of elements of type
// elem, and ensures that its elements are encoded.
//
// Preconditions: elem.size > 0, capacity > 0.
func (es *encodeState) sliceTarget(dataPtr unsafe.Pointer, elem *typeInfo, length, capacity uintptr) int32 {
	data := uintptr(dataPtr)
	esz := elem.size
	end := data + capacity*esz
	tid := es.lookup(data, elem)
	if tid >= 0 && es.target(tid).obj >= 0 {
		// Fast path: the slice is within the object of an existing target.
		r, off := es.root(es.target(tid).obj)
		off += es.target(tid).off
		if o := es.obj(r); o.addr+off+capacity*esz <= o.end {
			if o.isArray && o.ti == elem {
				es.cover(r, off/esz, off/esz+length)
			} else {
				es.checkInterior(r, off, elem, capacity)
			}
			return tid
		}
	}
	ov := es.query(data, end)
	if len(ov) == 1 && es.containsRange(ov[0], data, end, elem, capacity) {
		obj := ov[0]
		o := es.obj(obj)
		off := data - o.addr
		if o.isArray && o.ti == elem && off%esz == 0 {
			es.cover(obj, off/esz, off/esz+length)
		} else {
			es.checkInterior(obj, off, elem, capacity)
		}
		if tid < 0 {
			tid = es.newTarget(obj, off, elem)
			es.addLookup(data, tid)
		}
		return tid
	}
	// Create a new array object, merging arrays of the same element type
	// that partially overlap it.
	lo, hi := data, end
	for grown := true; grown; {
		grown = false
		for _, c := range es.query(lo, hi) {
			co := es.obj(c)
			if co.addr >= lo && co.end <= hi {
				continue
			}
			if !co.isArray || co.ti != elem || co.addr%esz != lo%esz {
				Failf("slice of %v at %#x overlaps %v at %#x", elem.typ, data, co.ti.typ, co.addr)
			}
			lo, hi = min(lo, co.addr), max(hi, co.end)
			grown = true
		}
	}
	obj := es.newContainer(unsafe.Add(dataPtr, -int(data-lo)), hi, elem, (hi-lo)/esz, true, es.query(lo, hi))
	k := (data - lo) / esz
	es.cover(obj, k, k+length)
	if tid < 0 || es.target(tid).obj < 0 {
		tid = es.newTarget(obj, data-lo, elem)
		es.addLookup(data, tid)
	}
	return tid
}

// mapTarget returns the target for a map.
func (es *encodeState) mapTarget(m unsafe.Pointer, ti *typeInfo) int32 {
	if tid, ok := es.maps[m]; ok {
		return tid
	}
	obj := es.newObject(m, uintptr(m)+1, ti, 1, false)
	es.obj(obj).isMap = true
	es.enqueue(obj, 0, 1)
	tid := es.newTarget(obj, 0, ti)
	es.maps[m] = tid
	return tid
}

// encodeValue encodes the value of type ti at p.
func (es *encodeState) encodeValue(ti *typeInfo, p unsafe.Pointer) {
	if uintptr(p) == es.spliceAt && es.trySplice(ti, 1) != 0 {
		return
	}
	switch ti.kind {
	case reflect.Bool, reflect.Int8, reflect.Uint8:
		es.out.byte(*(*uint8)(p))
	case reflect.Int:
		es.out.varint(int64(*(*int)(p)))
	case reflect.Int16:
		es.out.varint(int64(*(*int16)(p)))
	case reflect.Int32:
		es.out.varint(int64(*(*int32)(p)))
	case reflect.Int64:
		es.out.varint(*(*int64)(p))
	case reflect.Uint, reflect.Uintptr:
		es.out.uvarint(uint64(*(*uint)(p)))
	case reflect.Uint16:
		es.out.uvarint(uint64(*(*uint16)(p)))
	case reflect.Uint32:
		es.out.uvarint(uint64(*(*uint32)(p)))
	case reflect.Uint64:
		es.out.uvarint(*(*uint64)(p))
	case reflect.Float32:
		es.out.uint32(*(*uint32)(p))
	case reflect.Float64:
		es.out.uint64(*(*uint64)(p))
	case reflect.Complex64:
		es.out.raw(p, 8)
	case reflect.Complex128:
		es.out.raw(p, 16)
	case reflect.String:
		es.out.string(*(*string)(p))
	case reflect.Pointer:
		es.encodePointer(ti, *(*unsafe.Pointer)(p))
	case reflect.Slice:
		es.encodeSlice(ti, (*sliceHeader)(p))
	case reflect.Array:
		es.encodeElems(ti.elem, p, ti.length)
	case reflect.Map:
		es.encodeMapRef(ti, *(*unsafe.Pointer)(p))
	case reflect.Interface:
		es.encodeInterface(ti, p)
	case reflect.Struct:
		if ti.pod {
			es.out.raw(p, ti.size)
			return
		}
		es.encodeStruct(ti, p)
	default:
		Failf("unsupported type %v", ti.typ)
	}
}

func (es *encodeState) encodePointer(ti *typeInfo, v unsafe.Pointer) {
	if v == nil {
		es.out.byte(0)
		return
	}
	es.out.uvarint(uint64(es.ptrTarget(v, ti.elem)) + 1)
}

func (es *encodeState) encodeMapRef(ti *typeInfo, m unsafe.Pointer) {
	if m == nil {
		es.out.byte(0)
		return
	}
	es.out.uvarint(uint64(es.mapTarget(m, ti)) + 1)
}

func (es *encodeState) encodeSlice(ti *typeInfo, h *sliceHeader) {
	if h.data == nil {
		es.out.byte(0)
		return
	}
	es.out.uvarint(uint64(h.len) + 1)
	es.out.uvarint(uint64(h.cap - h.len))
	if h.cap == 0 || ti.elem.size == 0 {
		return
	}
	es.out.uvarint(uint64(es.sliceTarget(h.data, ti.elem, uintptr(h.len), uintptr(h.cap))))
}

// encodeElems encodes n elements of type elem at p.
func (es *encodeState) encodeElems(elem *typeInfo, p unsafe.Pointer, n uintptr) {
	if elem.pod {
		es.out.raw(p, elem.size*n)
		return
	}
	esz := elem.size
	for i := uintptr(0); i < n; i++ {
		q := add(p, i*esz)
		if uintptr(q) == es.spliceAt {
			if k := es.trySplice(elem, n-i); k != 0 {
				i += k - 1
				continue
			}
		}
		es.encodeValue(elem, q)
	}
}

func (es *encodeState) encodeInterface(ti *typeInfo, p unsafe.Pointer) {
	var (
		dyn  *typeInfo
		data unsafe.Pointer
	)
	if ti.emptyIface {
		e := (*eface)(p)
		if e.typ == nil {
			es.out.byte(0)
			return
		}
		dyn = es.typeCache.rtype(e.typ)
		data = e.data
	} else {
		i := (*iface)(p)
		if i.tab == nil {
			es.out.byte(0)
			return
		}
		dyn = es.typeCache.itab(p)
		data = i.data
	}
	es.out.uvarint(uint64(es.types.exactIndex(dyn)) + 1)
	es.encodeDynamic(dyn, data)
}

// encodeDynamic encodes a value of type ti held in an interface with the
// given data word.
func (es *encodeState) encodeDynamic(ti *typeInfo, data unsafe.Pointer) {
	if !ti.direct {
		es.encodeValueCopy(ti, data)
		return
	}
	switch ti.kind {
	case reflect.Pointer:
		es.encodePointer(ti, data)
	case reflect.Map:
		es.encodeMapRef(ti, data)
	default:
		w := new(unsafe.Pointer)
		*w = data
		es.encodeValue(ti, unsafe.Pointer(w))
	}
}

// encodeValueCopy encodes the value of type ti at p, which may be read-only.
func (es *encodeState) encodeValueCopy(ti *typeInfo, p unsafe.Pointer) {
	if !ti.inlineStruct {
		es.encodeValue(ti, p)
		return
	}
	// StateSave methods may modify the value, so encode a copy. Copies are
	// reused once encoded.
	free := es.copies[ti]
	var c reflect.Value
	if n := len(free); n > 0 {
		c = free[n-1]
		es.copies[ti] = free[:n-1]
	} else {
		c = reflect.New(ti.typ).Elem()
		if es.copies == nil {
			es.copies = make(map[*typeInfo][]reflect.Value)
		}
	}
	c.Set(reflect.NewAt(ti.typ, p).Elem())
	es.encodeValue(ti, c.Addr().UnsafePointer())
	c.SetZero()
	es.copies[ti] = append(es.copies[ti], c)
}

func (es *encodeState) encodeStruct(ti *typeInfo, p unsafe.Pointer) {
	switch ti.structKind {
	case structSaverLoader:
		var sl StructType
		*(*iface)(unsafe.Pointer(&sl)) = iface{tab: ti.methodItab, data: p}
		if es.stats.enabled {
			es.stats.start(ti)
			defer es.stats.done()
		}
		sl.StateSave(Sink{internal: objectEncoder{es: es, st: ti}})
	case structBinary:
		var bo binaryObject
		*(*iface)(unsafe.Pointer(&bo)) = iface{tab: ti.methodItab, data: p}
		data, err := bo.AppendBinary(es.binaryBuf[:0])
		es.binaryBuf = data
		if err != nil {
			Failf("encoding %s: %w", ti.name, err)
		}
		es.out.uvarint(uint64(len(data)))
		es.out.bytes(data)
	case structEmpty:
	default:
		Failf("struct %v does not implement SaverLoader", ti.typ)
	}
}

// encodeMap encodes the entries of a map, and returns the number of entries.
// The number of entries is in the object table, so that the decoder can
// allocate maps with the correct size.
func (es *encodeState) encodeMap(ti *typeInfo, m unsafe.Pointer) int {
	es.mapPtr = m
	v := reflect.NewAt(ti.typ, unsafe.Pointer(&es.mapPtr)).Elem()
	n := v.Len()
	if n == 0 {
		return 0
	}
	// Map encoding is not reentrant, so slots can be reused.
	sc, ok := es.mapSlots[ti]
	if !ok {
		sc.k = reflect.New(ti.key.typ).Elem()
		sc.e = reflect.New(ti.elem.typ).Elem()
		if es.mapSlots == nil {
			es.mapSlots = make(map[*typeInfo]mapSlots)
		}
		es.mapSlots[ti] = sc
	}
	k, e := sc.k, sc.e
	kp, ep := k.Addr().UnsafePointer(), e.Addr().UnsafePointer()
	count := 0
	var it reflect.MapIter
	it.Reset(v)
	for ; it.Next(); count++ {
		k.SetIterKey(&it)
		e.SetIterValue(&it)
		es.encodeValue(ti.key, kp)
		es.encodeValue(ti.elem, ep)
	}
	it.Reset(reflect.Value{})
	if count != n {
		Failf("map %v changed while saving", ti.typ)
	}
	return n
}

// mapSlots are storage for the keys and values of maps being encoded.
type mapSlots struct {
	k, e reflect.Value
}

// trySplice reuses the encoding of up to max values of type ti at
// es.spliceAt, if any. It returns the number of values encoded.
func (es *encodeState) trySplice(ti *typeInfo, max uintptr) uintptr {
	s := es.splices[es.spliceIdx]
	if s.ti != ti || s.count > max {
		return 0
	}
	es.out.bytes(es.out.b[s.seg.off : s.seg.off+s.seg.len])
	end := s.addr + ti.size*s.count
	es.spliceIdx++
	for es.spliceIdx < len(es.splices) && es.splices[es.spliceIdx].addr < end {
		es.spliceIdx++
	}
	es.spliceAt = noSplice
	if es.spliceIdx < len(es.splices) {
		es.spliceAt = es.splices[es.spliceIdx].addr
	}
	return s.count
}

// gatherSplices collects the encodings of the descendants of obj within
// [lo, hi) that must be reused.
func (es *encodeState) gatherSplices(obj int32, lo, hi uintptr) {
	for _, c := range es.obj(obj).childList() {
		co := es.obj(c)
		if co.end <= lo || co.addr >= hi {
			continue
		}
		if co.isArray {
			if co.ti.inlineStruct {
				for _, ch := range es.chunkList(co) {
					addr := co.addr + ch.start*co.ti.size
					if addr < lo || addr+(ch.end-ch.start)*co.ti.size > hi {
						continue
					}
					es.splices = append(es.splices, splice{
						addr:  addr,
						ti:    co.ti,
						count: ch.end - ch.start,
						seg:   ch.seg,
					})
				}
			}
		} else if co.state == objDone {
			if co.ti.inlineStruct && co.addr >= lo && co.end <= hi {
				es.splices = append(es.splices, splice{addr: co.addr, ti: co.ti, count: 1, seg: co.seg})
			}
			continue
		}
		es.gatherSplices(c, lo, hi)
	}
}

// setupSplices prepares the splices for encoding [lo, hi) of obj.
func (es *encodeState) setupSplices(obj int32, lo, hi uintptr) {
	if len(es.obj(obj).childList()) == 0 {
		return
	}
	es.gatherSplices(obj, lo, hi)
	if len(es.splices) == 0 {
		return
	}
	slices.SortFunc(es.splices, func(a, b splice) int {
		if a.addr != b.addr {
			return cmp.Compare(a.addr, b.addr)
		}
		return cmp.Compare(b.count*b.ti.size, a.count*a.ti.size)
	})
	es.spliceAt = es.splices[0].addr
}

// finishSplices checks that all splices were used.
func (es *encodeState) finishSplices() {
	if es.spliceIdx < len(es.splices) {
		s := &es.splices[es.spliceIdx]
		Failf("internal error: unused encoding of %v at %#x", s.ti.typ, s.addr)
	}
	es.splices = es.splices[:0]
	es.spliceIdx = 0
	es.spliceAt = noSplice
}

// sameTypeArrayChain returns true if obj and all objects that absorbed it are
// arrays of the same element type.
func (es *encodeState) sameTypeArrayChain(obj int32) bool {
	ti := es.obj(obj).ti
	for ; obj >= 0; obj = es.obj(obj).parent {
		if o := es.obj(obj); !o.isArray || o.ti != ti {
			return false
		}
	}
	return true
}

// process encodes queued work until none remains.
func (es *encodeState) process() {
	for es.queueHead < len(es.queue) {
		w := es.queue[es.queueHead]
		es.queueHead++
		o := es.obj(w.obj)
		if o.parent >= 0 && (!o.isArray || !es.sameTypeArrayChain(w.obj)) {
			// The object that absorbed this object encodes it.
			continue
		}
		start := len(es.out.b)
		if o.isArray {
			esz := o.ti.size
			lo, hi := o.addr+w.start*esz, o.addr+w.end*esz
			elem, p := o.ti, add(o.base, w.start*esz)
			es.setupSplices(w.obj, lo, hi)
			es.encodeElems(elem, p, w.end-w.start)
			es.finishSplices()
			o = es.obj(w.obj)
			es.addChunk(o, chunk{elemRange: w.elemRange, seg: segment{start, len(es.out.b) - start}})
			continue
		}
		o.state = objEncoding
		if o.isMap {
			n := es.encodeMap(o.ti, o.base)
			es.obj(w.obj).n = uintptr(n)
		} else {
			base, end, ti := o.base, o.end, o.ti
			es.setupSplices(w.obj, o.addr, end)
			es.encodeValue(ti, base)
			es.finishSplices()
		}
		o = es.obj(w.obj)
		o.seg = segment{start, len(es.out.b) - start}
		o.state = objDone
	}
}

// arrayChunks appends the encoded chunks of the live array obj and of the
// arrays of the same type that it absorbed.
func (es *encodeState) arrayChunks(obj int32, shift uintptr, out []chunk) []chunk {
	o := es.obj(obj)
	for _, ch := range es.chunkList(o) {
		ch.start += shift
		ch.end += shift
		out = append(out, ch)
	}
	for _, c := range o.childList() {
		if co := es.obj(c); co.isArray && co.ti == o.ti {
			out = es.arrayChunks(c, shift+co.parentOff/o.ti.size, out)
		}
	}
	return out
}

// Object kinds in the object table.
const (
	objKindValue = iota
	objKindArray
	objKindMap

	// objInexact is set on the kinds of objects whose types are described
	// only by their layout. Their data is decoded once the decoder knows
	// their type from a reference to them.
	objInexact = 0x80
)

// streamMagic starts every stream.
const streamMagic = 0x32534776

// save serializes the object graph rooted at the value of type ti at root,
// and appends it to w.
//
// The stream is:
//
//   - streamMagic, and the schema fingerprint of the binary.
//   - The type table: the types of objects and of the values of interfaces.
//   - The object table: the kind and type of each object.
//   - The target table: the object and offset of each pointer target.
//   - The data of each object, in order. Pointers are target indices.
func (es *encodeState) save(root unsafe.Pointer, ti *typeInfo, w *encBuf) {
	size := max(ti.size, 1)
	obj := es.newObject(root, uintptr(root)+size, ti, 1, false)
	if ti.size != 0 {
		es.index(obj)
	}
	es.enqueue(obj, 0, 1)
	es.process()
	if es.obj(0).parent >= 0 {
		Failf("root object %v is within another object", ti.typ)
	}

	// Resolve zero-sized targets within objects.
	for _, tid := range es.zeroTargets {
		t := es.target(tid)
		if t.addr == dummyAddr {
			continue
		}
		if ov := es.query(t.addr, t.addr+1); len(ov) == 1 {
			o := es.obj(ov[0])
			if off := t.addr - o.addr; o.isArray || es.contains(o.ti, t.ti, off, 1) {
				t.obj, t.off = ov[0], off
			}
		}
	}

	// Number the live objects.
	var n int32
	for i := int32(0); i < int32(es.objs.n); i++ {
		if es.obj(i).parent < 0 {
			es.obj(i).final = n
			n++
		}
	}

	// Object table.
	var tables encBuf
	tables.grow(int(n)*4 + es.targets.n*3 + 64)
	tables.uvarint(uint64(n))
	dataLen := 0
	for i := int32(0); i < int32(es.objs.n); i++ {
		o := es.obj(i)
		if o.parent >= 0 {
			continue
		}
		switch {
		case o.isMap:
			tables.byte(objKindMap)
			tables.uvarint(uint64(o.n))
		case o.isArray:
			idx, flag := es.types.objectIndex(o.ti)
			tables.byte(objKindArray | flag)
			tables.uvarint(uint64(idx))
			tables.uvarint(uint64(o.n))
		default:
			idx, flag := es.types.objectIndex(o.ti)
			if i == 0 {
				flag = 0
			}
			tables.byte(objKindValue | flag)
			tables.uvarint(uint64(idx))
		}
		dataLen += o.seg.len
		for _, ch := range es.chunkList(o) {
			dataLen += ch.seg.len + 8
		}
	}

	// Target table.
	tables.uvarint(uint64(es.targets.n))
	for i := int32(0); i < int32(es.targets.n); i++ {
		t := es.target(i)
		if t.obj < 0 {
			tables.byte(0)
			continue
		}
		r, off := es.root(t.obj)
		off += t.off
		final := uint64(es.obj(r).final) + 1
		if off == 0 {
			tables.uvarint(final << 1)
		} else {
			tables.uvarint(final<<1 | 1)
			tables.uvarint(uint64(off))
		}
	}

	w.grow(len(es.types.buf.b) + len(tables.b) + dataLen + 64)
	w.uint32(streamMagic)
	w.uint64(schemaFingerprint())
	w.uvarint(uint64(len(es.types.types)))
	w.bytes(es.types.buf.b)
	w.bytes(tables.b)
	var chunks []chunk
	for i := int32(0); i < int32(es.objs.n); i++ {
		o := es.obj(i)
		if o.parent >= 0 {
			continue
		}
		inexact := !o.isMap && i != 0 && es.types.isInexact(o.ti)
		if o.isMap || (inexact && !o.isArray) {
			// Maps are allocated, and inexact objects are decoded, once
			// referred to, which may be after their data, so their data is
			// skippable.
			w.uvarint(uint64(o.seg.len))
		}
		if !o.isArray {
			w.bytes(es.out.b[o.seg.off : o.seg.off+o.seg.len])
			continue
		}
		chunks = es.arrayChunks(int32(i), 0, chunks[:0])
		if inexact {
			// Prefix the data with its length.
			var sub encBuf
			es.writeChunks(&sub, o, chunks)
			w.uvarint(uint64(len(sub.b)))
			w.bytes(sub.b)
			continue
		}
		es.writeChunks(w, o, chunks)
	}
	es.stats.objects = int(n)
	setHints(ti, len(w.b), es.targets.n)
}

// writeChunks writes the chunks of an array object.
func (es *encodeState) writeChunks(w *encBuf, o *encObject, chunks []chunk) {
	if len(chunks) > 1 {
		slices.SortFunc(chunks, func(a, b chunk) int { return cmp.Compare(a.start, b.start) })
	}
	w.uvarint(uint64(len(chunks)))
	var last uintptr
	for j, ch := range chunks {
		if j > 0 && ch.start < chunks[j-1].end {
			Failf("internal error: overlapping chunks of %v", o.ti.typ)
		}
		w.uvarint(uint64(ch.start - last))
		w.uvarint(uint64(ch.end - ch.start))
		last = ch.end
		w.bytes(es.out.b[ch.seg.off : ch.seg.off+ch.seg.len])
	}
}

// typeTable assigns stream indices to types.
type typeTable struct {
	// exact and shape map typeInfo ids to indices plus one.
	exact, shape []int32
	types        []*typeInfo
	buf          encBuf
}

// Type descriptor tags.
const (
	typeNamed = iota
	typeBuiltin
	typePointer
	typeSlice
	typeArray
	typeMap
	typeAny
	typeEmptyStruct
)

// reset clears the table.
func (tt *typeTable) reset() {
	clear(tt.exact)
	clear(tt.shape)
	clear(tt.types)
	tt.types = tt.types[:0]
	tt.buf.b = tt.buf.b[:0]
}

func lookupIndex(m []int32, ti *typeInfo) (int, bool) {
	if int(ti.id) < len(m) {
		if v := m[ti.id]; v != 0 {
			return int(v - 1), true
		}
	}
	return 0, false
}

func setIndex(m *[]int32, ti *typeInfo, idx int) {
	for int(ti.id) >= len(*m) {
		*m = append(*m, make([]int32, len(*m)+256)...)
	}
	(*m)[ti.id] = int32(idx + 1)
}

// exactIndex returns the index of a type, which the decoder must be able to
// identify: named types must be registered.
func (tt *typeTable) exactIndex(ti *typeInfo) int {
	if idx, ok := lookupIndex(tt.exact, ti); ok {
		return idx
	}
	return tt.add(ti, true)
}

// shapeIndex returns the index of a type with the same memory layout as ti.
func (tt *typeTable) shapeIndex(ti *typeInfo) int {
	if idx, ok := lookupIndex(tt.shape, ti); ok {
		return idx
	}
	return tt.add(ti, false)
}

func (tt *typeTable) add(ti *typeInfo, exact bool) int {
	// Descriptors of composite types refer to the descriptors of their
	// elements, which must come first.
	registered := ti.registered
	var key, elem int
	if !registered {
		switch ti.kind {
		case reflect.Pointer, reflect.Slice, reflect.Array:
			elem = tt.index(ti.elem, exact)
		case reflect.Map:
			key = tt.index(ti.key, exact)
			elem = tt.index(ti.elem, exact)
		}
	}
	b := &tt.buf
	switch {
	case registered:
		b.byte(typeNamed)
		b.string(ti.name)
	case int(ti.kind) < len(builtinTypes) && builtinTypes[ti.kind] != nil:
		if exact && ti.typ != builtinTypes[ti.kind] {
			Failf("type %q is not known", ti.typ)
		}
		b.byte(typeBuiltin)
		b.byte(byte(ti.kind))
	case exact && ti.typ.Name() != "":
		Failf("type %q is not registered", ti.typ)
	case ti.kind == reflect.Pointer:
		b.byte(typePointer)
		b.uvarint(uint64(elem))
	case ti.kind == reflect.Slice:
		b.byte(typeSlice)
		b.uvarint(uint64(elem))
	case ti.kind == reflect.Array:
		b.byte(typeArray)
		b.uvarint(uint64(ti.length))
		b.uvarint(uint64(elem))
	case ti.kind == reflect.Map:
		b.byte(typeMap)
		b.uvarint(uint64(key))
		b.uvarint(uint64(elem))
	case ti.kind == reflect.Interface && (!exact || ti.typ == anyType):
		b.byte(typeAny)
	case ti.kind == reflect.Struct && ti.typ.NumField() == 0 && (!exact || ti.typ == emptyStructType):
		b.byte(typeEmptyStruct)
	default:
		Failf("type %q is not known", ti.typ)
	}
	idx := len(tt.types)
	tt.types = append(tt.types, ti)
	if exact || registered {
		setIndex(&tt.exact, ti, idx)
	}
	setIndex(&tt.shape, ti, idx)
	return idx
}

// canExact returns true if ti can be described exactly.
func (tt *typeTable) canExact(ti *typeInfo, depth int) bool {
	if depth > 32 {
		return false
	}
	switch {
	case ti.name != "":
		return ti.registered
	case int(ti.kind) < len(builtinTypes) && builtinTypes[ti.kind] != nil:
		return ti.typ == builtinTypes[ti.kind]
	case ti.typ.Name() != "":
		// Other named types must be registered.
		return false
	case ti.kind == reflect.Pointer, ti.kind == reflect.Slice, ti.kind == reflect.Array:
		return tt.canExact(ti.elem, depth+1)
	case ti.kind == reflect.Map:
		return tt.canExact(ti.key, depth+1) && tt.canExact(ti.elem, depth+1)
	case ti.kind == reflect.Interface:
		return ti.typ == anyType
	case ti.kind == reflect.Struct:
		return ti.typ == emptyStructType
	}
	return false
}

// isInexact returns true if objects of type ti are described inexactly, and
// must be decoded with their exact type.
func (tt *typeTable) isInexact(ti *typeInfo) bool {
	return ti.needsExact && !tt.canExact(ti, 0)
}

// objectIndex returns the index of the type of an object, and objInexact if
// the decoder must learn its exact type from references.
func (tt *typeTable) objectIndex(ti *typeInfo) (int, byte) {
	if tt.isInexact(ti) {
		return tt.shapeIndex(ti), objInexact
	}
	if ti.needsExact {
		return tt.exactIndex(ti), 0
	}
	return tt.shapeIndex(ti), 0
}

func (tt *typeTable) index(ti *typeInfo, exact bool) int {
	if exact {
		return tt.exactIndex(ti)
	}
	return tt.shapeIndex(ti)
}
