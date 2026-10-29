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
	"context"
	"fmt"
	"os"
	"reflect"
	"slices"
	"sync"
	"unsafe"
)

// decObject is an object being decoded.
type decObject struct {
	base unsafe.Pointer
	ti   *typeInfo
	// size is the size of the object in bytes.
	size uintptr
	kind uint8
	// mapType is the type of map objects, known once they are referred to.
	mapType *typeInfo
	// inexact is true for objects whose type is only known to have the
	// layout of ti. exact is their type, once known from a reference.
	inexact bool
	exact   *typeInfo
}

// callback is a callback run when an object is complete.
type callback struct {
	// fn is a user callback.
	fn func()
	// valueFn is called with value.
	valueFn func(any)
	value   any
	// afterLoader is a generated AfterLoad callback.
	afterLoader AfterLoader
	// valueLoader is called with slot and value.
	valueLoader ValueLoader
	slot        int
	// waiter is an object that waits for the object, or -1.
	waiter int32
	// next is the next callback of the same object, or -1.
	next int32
}

// objectDecodeState tracks the completion of an object.
//
// An object is complete once all objects it waits for are complete, at which
// point its callbacks run. Callbacks unblock objects waiting for it, and run
// AfterLoad functions.
type objectDecodeState struct {
	blockedBy int32
	// cbHead and cbTail delimit the list of callbacks, or are -1.
	cbHead, cbTail int32
	complete       bool
}

// decodeState is the state of Load.
type decodeState struct {
	ctx context.Context
	in  decBuf

	types []*typeInfo
	// typeNames are the names of named types, which may not be registered.
	typeNames               []string
	objs                    []decObject
	ods                     []objectDecodeState
	cbs                     []callback
	initialCallbackCapacity int
	callbackGrowth          []int
	targets                 []unsafe.Pointer
	// targetObjs are the objects of targets, or -1.
	targetObjs []int32

	// cur is the object being decoded.
	cur int32
	// lastRef is the object referred to by the last decoded pointer, slice,
	// map or interface, or -1.
	lastRef int32
	// keepZero is true while decoding the root object: like the zero values
	// of fields, which are not encoded, zero values do not overwrite the
	// root's existing state.
	keepZero bool

	// mapPtr holds the map being decoded.
	mapPtr unsafe.Pointer

	// noCallbacks disables user callbacks.
	noCallbacks bool

	// hooks counts the runs of code that may retain the addresses of
	// decoded values: callbacks, and the methods of structs with hooks.
	hooks uint64

	// leaves is the queue of objects whose callbacks can run.
	leaves []int32

	// strings is the current allocation for string data.
	strings []byte

	itabCache itabCache

	// valueScratch holds storage for values loaded by Source.LoadValueTo,
	// by type.
	valueScratch map[*typeInfo]unsafe.Pointer

	stats Stats
}

// decodeStatePool holds decodeStates for reuse.
var decodeStatePool sync.Pool

func newDecodeState(ctx context.Context) *decodeState {
	if ds, ok := decodeStatePool.Get().(*decodeState); ok {
		ds.ctx = ctx
		return ds
	}
	return &decodeState{ctx: ctx}
}

// release returns ds to decodeStatePool. Tables are cleared, so that they do
// not keep loaded objects alive.
func (ds *decodeState) release() {
	if os.Getenv("RESTORE_CALLBACK_SHAPE") == "1" {
		var wait, typed, fn, valueTyped, valueFn, other int
		for _, cb := range ds.cbs {
			switch {
			case cb.waiter >= 0:
				wait++
			case cb.afterLoader != nil:
				typed++
			case cb.fn != nil:
				fn++
			case cb.valueLoader != nil:
				valueTyped++
			case cb.valueFn != nil:
				valueFn++
			default:
				other++
			}
		}
		fmt.Printf("callback-shape objects=%d initial=%d count=%d final=%d sizeof=%d growth=%v wait=%d typed_after=%d fn=%d typed_value=%d value_fn=%d other=%d\n",
			len(ds.objs), ds.initialCallbackCapacity, len(ds.cbs), cap(ds.cbs), unsafe.Sizeof(callback{}), ds.callbackGrowth, wait, typed, fn, valueTyped, valueFn, other)
	}
	if cap(ds.objs) > maxPooledBytes/64 {
		return
	}
	clear(ds.types[:cap(ds.types)])
	clear(ds.typeNames[:cap(ds.typeNames)])
	clear(ds.objs[:cap(ds.objs)])
	clear(ds.cbs[:cap(ds.cbs)])
	clear(ds.targets[:cap(ds.targets)])
	*ds = decodeState{
		types:      ds.types[:0],
		typeNames:  ds.typeNames[:0],
		objs:       ds.objs[:0],
		ods:        ds.ods[:0],
		cbs:        ds.cbs[:0],
		targets:    ds.targets[:0],
		targetObjs: ds.targetObjs[:0],
		leaves:     ds.leaves[:0],
	}
	decodeStatePool.Put(ds)
}

// load decodes the stream in data into the value of type ti at root.
func (ds *decodeState) load(root unsafe.Pointer, ti *typeInfo) {
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

// readTypes reads the type table.
func (ds *decodeState) readTypes() {
	d := &ds.in
	n := d.count(1)
	ds.types = slices.Grow(ds.types[:0], n)[:n]
	ds.typeNames = slices.Grow(ds.typeNames[:0], n)[:n]
	elem := func() reflect.Type {
		i := d.uvarint()
		if i >= uint64(len(ds.types)) || ds.types[i] == nil {
			Failf("invalid type reference %d", i)
		}
		return ds.types[i].typ
	}
	for i := range ds.types {
		var t reflect.Type
		switch tag := d.byte(); tag {
		case typeNamed:
			// The name refers to the input, which outlives typeNames.
			nb := d.next(d.byteLength())
			name := unsafe.String(unsafe.SliceData(nb), len(nb))
			ds.typeNames[i] = name
			var ok bool
			if t = registeredTypeOf(name); t == nil {
				if t, ok = primitiveTypeDatabase[name]; !ok {
					// The type may be the root's, which need not be
					// registered.
					continue
				}
			}
		case typeBuiltin:
			k := d.byte()
			if int(k) >= len(builtinTypes) || builtinTypes[k] == nil {
				Failf("invalid builtin kind %d", k)
			}
			t = builtinTypes[k]
		case typePointer:
			t = reflect.PointerTo(elem())
		case typeSlice:
			t = reflect.SliceOf(elem())
		case typeArray:
			n := d.uvarint()
			if n > 1<<40 {
				Failf("invalid array length %d", n)
			}
			t = reflect.ArrayOf(int(n), elem())
		case typeMap:
			k := elem()
			t = reflect.MapOf(k, elem())
		case typeAny:
			t = anyType
		case typeEmptyStruct:
			t = emptyStructType
		default:
			Failf("invalid type tag %d", tag)
		}
		ds.types[i] = typeInfoOf(t)
	}
}

func (ds *decodeState) typeAt(i uint64) *typeInfo {
	if i >= uint64(len(ds.types)) {
		Failf("invalid type index %d", i)
	}
	if ds.types[i] == nil {
		Failf("type name %q is not available", ds.typeNames[i])
	}
	return ds.types[i]
}

// readObjects reads the object table, and allocates objects.
func (ds *decodeState) readObjects(root unsafe.Pointer, rootType *typeInfo) {
	d := &ds.in
	n := d.count(1)
	if n == 0 {
		Failf("no objects")
	}
	ds.initialCallbackCapacity = cap(ds.cbs)
	ds.objs = slices.Grow(ds.objs[:0], n)[:n]
	ds.ods = slices.Grow(ds.ods[:0], n)[:n]
	for i := range ds.objs {
		o := &ds.objs[i]
		ds.ods[i] = objectDecodeState{cbHead: -1, cbTail: -1}
		o.kind = d.byte()
		o.inexact = o.kind&objInexact != 0
		o.kind &^= objInexact
		switch o.kind {
		case objKindValue:
			if i == 0 {
				// The root's type is described by its shape, and its name
				// if it has one.
				idx := d.uvarint()
				if idx >= uint64(len(ds.types)) {
					Failf("invalid type index %d", idx)
				}
				t := ds.types[idx]
				if name := ds.typeNames[idx]; name != "" {
					if name != rootType.name {
						Failf("root has type %q, want %v", name, rootType.typ)
					}
				} else if t == nil || t.size != rootType.size || t.kind != rootType.kind {
					Failf("root has type %v, want %v", t.typ, rootType.typ)
				}
				o.ti = rootType
				o.size = o.ti.size
				o.base = root
				continue
			}
			o.ti = ds.typeAt(d.uvarint())
			o.size = o.ti.size
			{
				o.base = reflect.New(o.ti.typ).UnsafePointer()
			}
		case objKindArray:
			o.ti = ds.typeAt(d.uvarint())
			count := d.uvarint()
			if o.ti.size == 0 || count > (1<<40)/uint64(o.ti.size) {
				Failf("invalid array of %d %v", count, o.ti.typ)
			}
			o.size = uintptr(count) * o.ti.size
			o.base = reflect.MakeSlice(o.ti.sliceTypeOf(), int(count), int(count)).UnsafePointer()
		case objKindMap:
			if i == 0 {
				Failf("root is a map")
			}
			// size is the number of entries.
			count := d.uvarint()
			if count > 1<<40 {
				Failf("invalid map of %d entries", count)
			}
			o.size = uintptr(count)
		default:
			Failf("invalid object kind %d", o.kind)
		}
	}
}

// readTargets reads the target table.
func (ds *decodeState) readTargets() {
	d := &ds.in
	n := d.count(1)
	ds.targets = slices.Grow(ds.targets[:0], n)[:n]
	ds.targetObjs = slices.Grow(ds.targetObjs[:0], n)[:n]
	for i := range ds.targets {
		v := d.uvarint()
		if v == 0 {
			ds.targets[i] = unsafe.Pointer(&zeroSized)
			ds.targetObjs[i] = -1
			continue
		}
		obj := v>>1 - 1
		if obj >= uint64(len(ds.objs)) {
			Failf("invalid target object %d", obj)
		}
		var off uint64
		if v&1 != 0 {
			off = d.uvarint()
		}
		o := &ds.objs[obj]
		if o.kind == objKindMap {
			if off != 0 {
				Failf("invalid map target")
			}
		} else if off > uint64(o.size) {
			Failf("invalid target offset %d in %v", off, o.ti.typ)
		} else {
			ds.targets[i] = add(o.base, uintptr(off))
		}
		ds.targetObjs[i] = int32(obj)
	}
}

// target returns the target with the given index plus one, which must be
// non-zero.
func (ds *decodeState) target(v uint64) (unsafe.Pointer, int32) {
	if v == 0 || v > uint64(len(ds.targets)) {
		Failf("invalid target %d", v)
	}
	return ds.targets[v-1], ds.targetObjs[v-1]
}

// decodeObjects decodes the data of all objects.
func (ds *decodeState) decodeObjects() {
	d := &ds.in
	var deferred []deferredObject
	for i := range ds.objs {
		ds.cur = int32(i)
		o := &ds.objs[i]
		if o.kind == objKindMap || (o.inexact && o.kind == objKindValue) {
			n := d.byteLength()
			if !ds.decodable(o) {
				// Decode the object once it has been referred to.
				deferred = append(deferred, deferredObject{obj: int32(i), pos: d.pos, end: d.pos + n})
				d.pos += n
				continue
			}
		} else if o.inexact && !ds.decodable(o) {
			n := d.byteLength()
			deferred = append(deferred, deferredObject{obj: int32(i), pos: d.pos, end: d.pos + n})
			d.pos += n
			continue
		} else if o.inexact {
			d.byteLength()
		}
		ds.decodeObject(int32(i))
	}
	for len(deferred) > 0 {
		remaining := deferred[:0]
		for _, do := range deferred {
			if !ds.decodable(&ds.objs[do.obj]) {
				remaining = append(remaining, do)
				continue
			}
			pos := d.pos
			d.pos = do.pos
			ds.cur = do.obj
			ds.decodeObject(do.obj)
			if d.pos != do.end {
				Failf("object %d has %d bytes of trailing data", do.obj, do.end-d.pos)
			}
			d.pos = pos
		}
		if len(remaining) == len(deferred) {
			Failf("the type of object %d (%s) is not known from references to it", remaining[0].obj, ds.objTypeName(remaining[0].obj))
		}
		deferred = remaining
	}
}

// decodable returns true if the type needed to decode o is known.
func (ds *decodeState) decodable(o *decObject) bool {
	if o.kind == objKindMap {
		return o.mapType != nil
	}
	return !o.inexact || o.exact != nil
}

// decodeObject decodes the data of the object obj.
func (ds *decodeState) decodeObject(obj int32) {
	d := &ds.in
	o := &ds.objs[obj]
	ti := o.ti
	if o.exact != nil {
		ti = o.exact
	}
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
		ds.decodeMap(o.mapType, o.base, int(o.size))
	}
}

// noteRef notes a reference to a value of type ti at t in the object obj,
// which tells the exact type of inexact objects.
func (ds *decodeState) noteRef(obj int32, t unsafe.Pointer, ti *typeInfo) {
	o := &ds.objs[obj]
	if !o.inexact || o.exact != nil || ti.size != o.ti.size || ti.kind != o.ti.kind {
		return
	}
	off := uintptr(t) - uintptr(o.base)
	if off%ti.size == 0 && (o.kind == objKindArray || off == 0) {
		o.exact = ti
	}
}

// deferredObject is the data of an object that is decoded after other
// objects.
type deferredObject struct {
	obj      int32
	pos, end int
}

// decodeValue decodes a value of type ti to p.
func (ds *decodeState) decodeValue(ti *typeInfo, p unsafe.Pointer) {
	d := &ds.in
	switch ti.kind {
	case reflect.Bool, reflect.Int8, reflect.Uint8:
		if v := d.byte(); v != 0 || !ds.keepZero {
			*(*uint8)(p) = v
		}
	case reflect.Int:
		if v := d.varint(); v != 0 || !ds.keepZero {
			*(*int)(p) = int(v)
		}
	case reflect.Int16:
		if v := d.varint(); v != 0 || !ds.keepZero {
			*(*int16)(p) = int16(v)
		}
	case reflect.Int32:
		if v := d.varint(); v != 0 || !ds.keepZero {
			*(*int32)(p) = int32(v)
		}
	case reflect.Int64:
		if v := d.varint(); v != 0 || !ds.keepZero {
			*(*int64)(p) = v
		}
	case reflect.Uint, reflect.Uintptr:
		if v := d.uvarint(); v != 0 || !ds.keepZero {
			*(*uint)(p) = uint(v)
		}
	case reflect.Uint16:
		if v := d.uvarint(); v != 0 || !ds.keepZero {
			*(*uint16)(p) = uint16(v)
		}
	case reflect.Uint32:
		if v := d.uvarint(); v != 0 || !ds.keepZero {
			*(*uint32)(p) = uint32(v)
		}
	case reflect.Uint64:
		if v := d.uvarint(); v != 0 || !ds.keepZero {
			*(*uint64)(p) = v
		}
	case reflect.Float32:
		if v := d.uint32(); v != 0 || !ds.keepZero {
			*(*uint32)(p) = v
		}
	case reflect.Float64:
		if v := d.uint64(); v != 0 || !ds.keepZero {
			*(*uint64)(p) = v
		}
	case reflect.Complex64, reflect.Complex128:
		b := d.next(int(ti.size))
		if !ds.keepZero || !isZero(b) {
			copy(unsafe.Slice((*byte)(p), ti.size), b)
		}
	case reflect.String:
		if n := d.byteLength(); n != 0 {
			*(*string)(p) = ds.makeString(d.next(n))
		} else if !ds.keepZero {
			*(*string)(p) = ""
		}
	case reflect.Pointer:
		v := d.uvarint()
		if v == 0 {
			if !ds.keepZero {
				*(*unsafe.Pointer)(p) = nil
			}
			return
		}
		t, obj := ds.target(v)
		if t == nil {
			Failf("pointer %v to map", ti.typ)
		}
		*(*unsafe.Pointer)(p) = t
		ds.lastRef = obj
		if obj >= 0 && ds.objs[obj].inexact {
			ds.noteRef(obj, t, ti.elem)
		}
	case reflect.Slice:
		ds.decodeSlice(ti, (*sliceHeader)(p))
	case reflect.Array:
		ds.decodeArray(ti, p)
	case reflect.Map:
		v := d.uvarint()
		if v == 0 {
			if !ds.keepZero {
				*(*unsafe.Pointer)(p) = nil
			}
			return
		}
		*(*unsafe.Pointer)(p) = ds.mapRef(ti, v)
	case reflect.Interface:
		ds.decodeInterface(ti, p)
	case reflect.Struct:
		if ti.pod {
			b := d.next(int(ti.size))
			if !ds.keepZero || !isZero(b) {
				copy(unsafe.Slice((*byte)(p), ti.size), b)
			}
			return
		}
		ds.decodeStruct(ti, p)
		ds.lastRef = -1
	default:
		Failf("unsupported type %v", ti.typ)
	}
}

func isZero(b []byte) bool {
	for _, c := range b {
		if c != 0 {
			return false
		}
	}
	return true
}

// makeString returns a string with the contents of b.
func (ds *decodeState) makeString(b []byte) string {
	const chunkSize = 16 << 10
	if len(b) > chunkSize/4 {
		return string(b)
	}
	if len(ds.strings) < len(b) {
		ds.strings = make([]byte, chunkSize)
	}
	s := ds.strings[:len(b):len(b)]
	ds.strings = ds.strings[len(b):]
	copy(s, b)
	return unsafe.String(unsafe.SliceData(s), len(s))
}

// mapRef returns the map with the given target index plus one, allocating it
// if needed.
func (ds *decodeState) mapRef(ti *typeInfo, v uint64) unsafe.Pointer {
	_, obj := ds.target(v)
	if obj < 0 || ds.objs[obj].kind != objKindMap {
		Failf("map %v is not a map object", ti.typ)
	}
	o := &ds.objs[obj]
	if o.mapType == nil {
		o.mapType = ti
		// Limit the size hint of invalid streams by their length.
		hint := min(int(o.size), len(ds.in.b))
		o.base = reflect.MakeMapWithSize(ti.typ, hint).UnsafePointer()
	} else if o.mapType != ti {
		Failf("map is both %v and %v", o.mapType.typ, ti.typ)
	}
	ds.lastRef = obj
	return o.base
}

func (ds *decodeState) decodeSlice(ti *typeInfo, h *sliceHeader) {
	d := &ds.in
	v := d.uvarint()
	if v == 0 {
		if !ds.keepZero {
			*h = sliceHeader{}
		}
		return
	}
	length := v - 1
	capacity := length + d.uvarint()
	if capacity < length || capacity > 1<<40 {
		Failf("invalid slice capacity %d", capacity)
	}
	esz := ti.elem.size
	if capacity == 0 || esz == 0 {
		*h = sliceHeader{data: unsafe.Pointer(&zeroSized), len: int(length), cap: int(capacity)}
		return
	}
	t, obj := ds.target(d.uvarint() + 1)
	if obj < 0 || t == nil {
		Failf("invalid slice target")
	}
	o := &ds.objs[obj]
	if uintptr(t)+uintptr(capacity)*esz > uintptr(o.base)+o.size {
		Failf("slice of %d %v exceeds object", capacity, ti.elem.typ)
	}
	*h = sliceHeader{data: t, len: int(length), cap: int(capacity)}
	ds.lastRef = obj
	if o.inexact {
		ds.noteRef(obj, t, ti.elem)
	}
}

// decodeArray decodes an array value.
func (ds *decodeState) decodeArray(ti *typeInfo, p unsafe.Pointer) {
	if ti.elem.pod && ds.keepZero {
		b := ds.in.next(int(ti.size))
		if !isZero(b) {
			copy(unsafe.Slice((*byte)(p), ti.size), b)
		}
		return
	}
	keepZero := ds.keepZero
	ds.keepZero = false
	ds.decodeElems(ti.elem, p, ti.length)
	ds.keepZero = keepZero
}

// decodeElems decodes n elements of type elem to p. The current object waits
// for the objects referred to by the elements.
func (ds *decodeState) decodeElems(elem *typeInfo, p unsafe.Pointer, n uintptr) {
	if elem.pod {
		ds.in.raw(p, elem.size*n)
		return
	}
	esz := elem.size
	for i := uintptr(0); i < n; i++ {
		ds.lastRef = -1
		ds.decodeValue(elem, add(p, i*esz))
		if ds.lastRef >= 0 {
			ds.wait(ds.cur, ds.lastRef, callback{waiter: -1})
		}
	}
	ds.lastRef = -1
}

// decodeInterface decodes an interface value.
func (ds *decodeState) decodeInterface(ti *typeInfo, p unsafe.Pointer) {
	v := ds.in.uvarint()
	if v == 0 {
		if !ds.keepZero {
			*(*iface)(p) = iface{}
		}
		return
	}
	dyn := ds.typeAt(v - 1)
	keepZero := ds.keepZero
	ds.keepZero = false
	data := ds.decodeDynamic(dyn)
	ds.keepZero = keepZero
	if ti.emptyIface {
		*(*eface)(p) = eface{typ: dyn.rtype, data: data}
	} else {
		*(*iface)(p) = iface{tab: ds.itabCache.get(ti, dyn), data: data}
	}
}

// decodeDynamic decodes a value of type ti, and returns the data word of
// interfaces holding it.
func (ds *decodeState) decodeDynamic(ti *typeInfo) unsafe.Pointer {
	if !ti.direct {
		q := reflect.New(ti.typ).UnsafePointer()
		ds.decodeValue(ti, q)
		return q
	}
	switch ti.kind {
	case reflect.Pointer:
		v := ds.in.uvarint()
		if v == 0 {
			return nil
		}
		t, obj := ds.target(v)
		ds.lastRef = obj
		if obj >= 0 && ds.objs[obj].inexact {
			ds.noteRef(obj, t, ti.elem)
		}
		return t
	case reflect.Map:
		v := ds.in.uvarint()
		if v == 0 {
			return nil
		}
		return ds.mapRef(ti, v)
	default:
		w := new(unsafe.Pointer)
		ds.decodeValue(ti, unsafe.Pointer(w))
		return *w
	}
}

func (ds *decodeState) decodeStruct(ti *typeInfo, p unsafe.Pointer) {
	switch ti.structKind {
	case structSaverLoader:
		if ti.hooks {
			ds.hooks++
		}
		var sl StructType
		*(*iface)(unsafe.Pointer(&sl)) = iface{tab: ti.methodItab, data: p}
		if ds.stats.enabled {
			ds.stats.start(ti)
			defer ds.stats.done()
		}
		sl.StateLoad(ds.ctx, Source{internal: objectDecoder{ds: ds, st: ti}})
	case structBinary:
		ds.hooks++
		data := ds.in.next(ds.in.byteLength())
		var bo binaryObject
		*(*iface)(unsafe.Pointer(&bo)) = iface{tab: ti.methodItab, data: p}
		if err := bo.UnmarshalBinary(data); err != nil {
			Failf("decoding %s: %w", ti.name, err)
		}
	case structEmpty:
	default:
		Failf("struct %v does not implement SaverLoader", ti.typ)
	}
}

// decodeMap decodes the n entries of a map. The map waits for the objects
// referred to by its entries.
func (ds *decodeState) decodeMap(ti *typeInfo, m unsafe.Pointer, n int) {
	if n == 0 {
		return
	}
	ds.mapPtr = m
	mv := reflect.NewAt(ti.typ, unsafe.Pointer(&ds.mapPtr)).Elem()
	kt, et := ti.key, ti.elem
	var (
		k, e   reflect.Value
		kp, ep unsafe.Pointer
	)
	// Callbacks and the StateLoad methods of structs with hooks may retain
	// the addresses of keys and values. Entries get fresh storage after
	// those run; other entries reuse the storage of the previous entry.
	fresh := true
	for i := 0; i < n; i++ {
		if fresh {
			k = reflect.New(kt.typ).Elem()
			e = reflect.New(et.typ).Elem()
			kp, ep = k.Addr().UnsafePointer(), e.Addr().UnsafePointer()
		} else {
			// Decoding does not write fields that are not saved.
			if kt.partial {
				k.SetZero()
			}
			if et.partial {
				e.SetZero()
			}
		}
		hooks := ds.hooks
		ds.lastRef = -1
		ds.decodeValue(kt, kp)
		if ds.lastRef >= 0 {
			ds.wait(ds.cur, ds.lastRef, callback{waiter: -1})
		}
		ds.lastRef = -1
		ds.decodeValue(et, ep)
		if ds.lastRef >= 0 {
			ds.wait(ds.cur, ds.lastRef, callback{waiter: -1})
		}
		mv.SetMapIndex(k, e)
		fresh = ds.hooks != hooks
	}
	ds.lastRef = -1
}

// user returns true if cb runs user code.
func (cb *callback) user() bool {
	return cb.fn != nil || cb.valueFn != nil || cb.afterLoader != nil || cb.valueLoader != nil
}

// loadValue decodes a value of type ti for Source.LoadValueTo, and calls
// l.StateLoadValue with it once the objects it refers to are complete.
func (ds *decodeState) loadValue(ti *typeInfo, slot int, l ValueLoader) {
	// Values are decoded to storage that is reused while the values are
	// only passed to l immediately: generated StateLoadValue methods copy
	// them. Values passed later, or whose decoding ran code that may keep
	// their address, keep their storage.
	p, ok := ds.valueScratch[ti]
	if !ok {
		p = reflect.New(ti.typ).UnsafePointer()
		if ds.valueScratch == nil {
			ds.valueScratch = make(map[*typeInfo]unsafe.Pointer)
		}
		ds.valueScratch[ti] = p
	} else if ti.partial {
		reflect.NewAt(ti.typ, p).Elem().SetZero()
	}
	hooks := ds.hooks
	ds.lastRef = -1
	keepZero := ds.keepZero
	ds.keepZero = false
	ds.decodeValue(ti, p)
	ds.keepZero = keepZero
	cb := callback{valueLoader: l, slot: slot, value: makeAny(ti, p), waiter: -1}
	if ds.lastRef >= 0 && ds.lastRef != ds.cur && ds.lastRef != 0 {
		delete(ds.valueScratch, ti)
		ds.wait(ds.cur, ds.lastRef, cb)
		return
	}
	if ds.hooks != hooks {
		delete(ds.valueScratch, ti)
	}
	ds.runCallback(&cb)
}

// addCallback adds a callback to obj.
func (ds *decodeState) addCallback(obj int32, cb callback) {
	if cb.waiter < 0 {
		ds.hooks++
	}
	cb.next = -1
	oldCap := cap(ds.cbs)
	ds.cbs = append(ds.cbs, cb)
	if cap(ds.cbs) != oldCap {
		ds.callbackGrowth = append(ds.callbackGrowth, cap(ds.cbs))
	}
	i := int32(len(ds.cbs) - 1)
	o := &ds.ods[obj]
	if o.cbTail < 0 {
		o.cbHead = i
	} else {
		ds.cbs[o.cbTail].next = i
	}
	o.cbTail = i
}

// runCallback runs cb if it is a user callback.
func (ds *decodeState) runCallback(cb *callback) {
	if ds.noCallbacks {
		return
	}
	switch {
	case cb.fn != nil:
		ds.hooks++
		cb.fn()
	case cb.valueFn != nil:
		ds.hooks++
		cb.valueFn(cb.value)
	case cb.afterLoader != nil:
		ds.hooks++
		cb.afterLoader.StateAfterLoad(ds.ctx)
	case cb.valueLoader != nil:
		ds.hooks++
		cb.valueLoader.StateLoadValue(ds.ctx, cb.slot, cb.value)
	}
}

// wait makes waiter wait for obj, and runs cb (if it is a user callback) once
// obj is complete.
//
// As special cases, objects may refer to themselves and to the root object
// without waiting, since the root object may have fields that are already
// decoded.
func (ds *decodeState) wait(waiter, obj int32, cb callback) {
	if obj == waiter || obj == 0 {
		ds.runCallback(&cb)
		return
	}
	ds.ods[waiter].blockedBy++
	if cb.user() {
		ds.addCallback(obj, cb)
	}
	ds.addCallback(obj, callback{waiter: waiter})
}

// runCallbacks runs the callbacks of objects as they become complete.
func (ds *decodeState) runCallbacks() {
	leaves := ds.leaves[:0]
	defer func() { ds.leaves = leaves }()
	for i := range ds.ods {
		if ds.ods[i].blockedBy == 0 {
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

func (ds *decodeState) objTypeName(obj int32) string {
	o := &ds.objs[obj]
	switch o.kind {
	case objKindArray:
		return fmt.Sprintf("[%d]%v", o.size/o.ti.size, o.ti.typ)
	case objKindMap:
		if o.mapType != nil {
			return o.mapType.typ.String()
		}
		return "map"
	}
	return o.ti.typ.String()
}

// findCycle finds a dependency cycle after all unblocked objects have
// completed. Every remaining object has a blocker that is also incomplete.
// Following one blocker per object must reach a cycle, even if the starting
// object is only waiting on that cycle.
func (ds *decodeState) findCycle() []int32 {
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
