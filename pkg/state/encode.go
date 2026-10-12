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
	"context"
	"reflect"
	"sort"

	"gvisor.dev/gvisor/pkg/state/wire"
)

// objectEncodeState the type and identity of an object occupying a memory
// address range. This is the value type for addrSet, and the intrusive entry
// for the deferred list.
type objectEncodeState struct {
	// id is the assigned ID for this object.
	id objectID

	// obj is the object value. Note that this may be replaced if we
	// encounter an object that contains this object. When this happens (in
	// resolve), we will update existing references appropriately, below,
	// and defer a re-encoding of the object.
	obj objectValue

	// encoded is the encoded value of this object. Note that this may not
	// be up to date if this object is still in the deferred list.
	encoded wire.Object

	// how indicates whether this object should be encoded as a value. This
	// is used only for deferred encoding.
	how encodeStrategy

	// refs are the list of reference objects used by other objects
	// referring to this object. When the object is updated, these
	// references may be updated directly and automatically.
	refs []*wire.Ref

	deferredEntry
}

// encodeState is state used for encoding.
//
// The encoding process constructs a representation of the in-memory graph of
// objects before a single object is serialized. This is done to ensure that
// all references can be fully disambiguated. See resolve for more details.
type encodeState struct {
	// ctx is the encode context.
	ctx context.Context

	// w is the output stream.
	w wire.Writer

	// types is the type database.
	types typeEncodeDatabase

	// lastID is the last allocated object ID.
	lastID objectID

	// values tracks the address ranges occupied by objects, along with the
	// types of these objects. This is used to locate pointer targets,
	// including pointers to fields within another type.
	//
	// Multiple objects may overlap in memory iff the larger object fully
	// contains the smaller one, and the type of the smaller object matches
	// a field or array element's type at the appropriate offset. An
	// arbitrary number of objects may be nested in this manner.
	//
	// Note that this does not track zero-sized objects, those are tracked
	// by zeroValues below.
	values addrSet

	// zeroValues tracks zero-sized objects.
	zeroValues map[reflect.Type]*objectEncodeState

	// deferred is the list of objects to be encoded.
	deferred deferredList

	// pendingTypes is the list of types to be serialized. Serialization
	// will occur when all objects have been encoded, but before pending is
	// serialized.
	pendingTypes []wire.Type

	// pending maps object IDs to objects to be serialized. Serialization does
	// not actually occur until the full object graph is computed.
	pending map[objectID]*objectEncodeState

	// encodedStructs maps reflect.Values representing structs to previous
	// encodings of those structs. This is necessary to avoid duplicate calls
	// to SaverLoader.StateSave() that may result in multiple calls to
	// Sink.SaveValue() for a given field, resulting in object duplication.
	encodedStructs map[reflect.Value]*wire.Record

	// binaryBuf holds temporary BinaryAppender output. Each saved value is
	// copied into an immutable string before this buffer is reused.
	binaryBuf []byte

	// stats tracks time data.
	stats Stats
}

// isSameSizeParent returns true if child is a field value or element within
// parent. Only a struct or array can have a child value.
//
// isSameSizeParent deals with objects like this:
//
//	struct child {
//		// fields..
//	}
//
//	struct parent {
//		c child
//	}
//
// var p parent
// record(&p.c)
//
// Here, &p and &p.c occupy the exact same address range.
//
// Or like this:
//
//	struct child {
//		// fields
//	}
//
// var arr [1]parent
// record(&arr[0])
//
// Similarly, &arr[0] and &arr[0].c have the exact same address range.
//
// Precondition: parent and child must occupy the same memory.
func isSameSizeParent(parent objectValue, child *typeLayout) bool {
	return parent.ops.layout.find(child, 0, nil)
}

// nextID returns the next valid ID.
func (es *encodeState) nextID() objectID {
	es.lastID++
	return objectID(es.lastID)
}

// dummyAddr points to the dummy zero-sized address.
var dummyAddr = reflect.ValueOf(new(struct{})).Pointer()

// resolveMap adapts the public map-header boundary without inspecting Go's
// private map representation. Identity is fixed now; the deferred source may
// still be an addressed header that a later hook reassigns.
func (es *encodeState) resolveMap(obj reflect.Value, ref *wire.Ref) {
	es.resolveMapValue(reflectedValue(obj), ref)
}

func (es *encodeState) resolveMapValue(obj value, ref *wire.Ref) {
	obj.require(reflect.Map)
	es.resolveValue(obj, uintptr(obj.pointer()), ref, encodeMapAsValue)
}

// resolveValue is the sole graph resolver. address is the identity captured now;
// an addressed map's value still reads its live header when deferred capture runs.
func (es *encodeState) resolveValue(obj value, addr uintptr, ref *wire.Ref, how encodeStrategy) {

	// Is this a map pointer? Just record the single address. It is not
	// possible to take any pointers into the map internals.
	if how == encodeMapAsValue {
		if addr == 0 {
			// Just leave the nil reference alone. This is fine, we
			// may need to encode as a reference in this way. We
			// return nil for our objectEncodeState so that anyone
			// depending on this value knows there's nothing there.
			return
		}
		seg, gap := es.values.Find(addr)
		if seg.Ok() {
			// Ensure the map types match.
			existing := seg.Value()
			if existing.obj.ops.layout.typ != obj.ops.layout.typ {
				Failf("overlapping map objects at 0x%x: [new object] %#v [existing object type] %s", addr, obj, existing.obj.ops.layout.typ)
			}

			// No sense recording refs, maps may not be replaced by
			// covering objects, they are maximal.
			ref.Root = wire.Uint(existing.id)
			return
		}

		// Copy a by-value header only for a newly discovered map. Addressed
		// headers retain the caller's actual slot and its deferred reassignment
		// behavior; aliases found above require no additional storage.
		if obj.address == nil {
			copy := copyValue(obj.reflect())
			obj.address = copy.Addr().UnsafePointer()
		}
		// Record the map.
		r := addrRange{addr, addr + 1}
		oes := &objectEncodeState{
			id:  es.nextID(),
			obj: objectValue{ops: obj.ops, address: obj.address},
			how: encodeMapAsValue,
		}
		// Use Insert instead of InsertWithoutMergingUnchecked when race
		// detection is enabled to get additional sanity-checking from Merge.
		if !raceEnabled {
			es.values.InsertWithoutMergingUnchecked(gap, r, oes)
		} else {
			es.values.Insert(gap, r, oes)
		}
		es.pending[oes.id] = oes
		es.deferred.PushBack(oes)

		// See above: no ref recording.
		ref.Root = wire.Uint(oes.id)
		return
	}

	// Is this a zero-sized type?
	typ := obj.ops.layout.typ
	size := obj.ops.layout.size
	if size == 0 {
		if addr == dummyAddr {
			// Zero-sized objects point to a dummy byte within the
			// runtime.  There's no sense recording this in the
			// address map.  We add this to the dedicated
			// zeroValues.
			//
			// Note that zero-sized objects must be *true*
			// zero-sized objects. They cannot be part of some
			// larger object. Interior zero-sized fields are represented
			// by a one-byte address range below.
			oes, ok := es.zeroValues[typ]
			if !ok {
				oes = &objectEncodeState{
					id:  es.nextID(),
					obj: objectValue{ops: obj.ops, address: obj.address},
				}
				es.zeroValues[typ] = oes
				es.pending[oes.id] = oes
				es.deferred.PushBack(oes)
			}

			// There's also no sense tracking back references. We
			// know that this is a true zero-sized object, and not
			// part of a larger container, so it will not change.
			ref.Root = wire.Uint(oes.id)
			return
		}
		size = 1 // See above.
	}

	end := addr + size
	r := addrRange{addr, end}
	seg := es.values.LowerBoundSegment(addr)
	var (
		oes *objectEncodeState
		gap addrGapIterator
	)

	// Does at least one previously-registered object overlap this one?
	if seg.Ok() && seg.Start() < end {
		existing := seg.Value()

		if seg.Range() == r && typ == existing.obj.ops.layout.typ {
			// This exact object is already registered. Avoid the traversal and
			// just return directly. We don't need to encode the type
			// information or any dots here.
			ref.Root = wire.Uint(existing.id)
			existing.refs = append(existing.refs, ref)
			return
		}

		if seg.Range().IsSupersetOf(r) && (seg.Range() != r || isSameSizeParent(existing.obj, obj.ops.layout)) {
			// This object is contained within a previously-registered object.
			// Perform traversal from the container to the new object.
			ref.Root = wire.Uint(existing.id)
			ref.Dots = traverseLayouts(existing.obj.ops.layout, obj.ops.layout, seg.Start(), addr)
			ref.Type = es.findType(existing.obj.ops.layout.typ)
			existing.refs = append(existing.refs, ref)
			return
		}

		// This object contains one or more previously-registered objects.
		// Remove them and update existing references to use the new one.
		oes := &objectEncodeState{
			// Reuse the root ID of the first contained element.
			id:  existing.id,
			obj: objectValue{ops: obj.ops, address: obj.address},
		}
		type elementEncodeState struct {
			addr   uintptr
			layout *typeLayout
			refs   []*wire.Ref
		}
		var (
			elems []elementEncodeState
			gap   addrGapIterator
		)
		for {
			// Each contained object should be completely contained within
			// this one.
			if raceEnabled && !r.IsSupersetOf(seg.Range()) {
				Failf("containing object %#v does not contain existing object %#v", obj, existing.obj)
			}
			elems = append(elems, elementEncodeState{
				addr:   seg.Start(),
				layout: existing.obj.ops.layout,
				refs:   existing.refs,
			})
			delete(es.pending, existing.id)
			es.deferred.Remove(existing)
			gap = es.values.Remove(seg)
			seg = gap.NextSegment()
			if !seg.Ok() || seg.Start() >= end {
				break
			}
			existing = seg.Value()
		}
		wt := es.findType(typ)
		for _, elem := range elems {
			dots := traverseLayouts(obj.ops.layout, elem.layout, addr, elem.addr)
			for _, ref := range elem.refs {
				ref.Root = wire.Uint(oes.id)
				ref.Dots = append(ref.Dots, dots...)
				ref.Type = wt
			}
			oes.refs = append(oes.refs, elem.refs...)
		}
		// Finally register the new containing object.
		if !raceEnabled {
			es.values.InsertWithoutMergingUnchecked(gap, r, oes)
		} else {
			es.values.Insert(gap, r, oes)
		}
		es.pending[oes.id] = oes
		es.deferred.PushBack(oes)
		ref.Root = wire.Uint(oes.id)
		oes.refs = append(oes.refs, ref)
		return
	}

	// No existing object overlaps this one. Register a new object.
	oes = &objectEncodeState{
		id:  es.nextID(),
		obj: objectValue{ops: obj.ops, address: obj.address},
	}
	if seg.Ok() {
		gap = seg.PrevGap()
	} else {
		gap = es.values.LastGap()
	}
	if !raceEnabled {
		es.values.InsertWithoutMergingUnchecked(gap, r, oes)
	} else {
		es.values.Insert(gap, r, oes)
	}
	es.pending[oes.id] = oes
	es.deferred.PushBack(oes)
	ref.Root = wire.Uint(oes.id)
	oes.refs = append(oes.refs, ref)
}

// traverseLayouts searches for a target object within a root object, where the target
// object is a struct field or array element within root, with potentially
// multiple intervening types. traverseLayouts returns the set of field or element
// traversals required to reach the target.
//
// Note that for efficiency, traverseLayouts returns the dots in the reverse order.
// That is, the first traversal required will be the last element of the list.
//
// Precondition: The target object must lie completely within the range defined
// by [rootAddr, rootAddr + root.size].
func traverseLayouts(root, target *typeLayout, rootAddr, targetAddr uintptr) []wire.Dot {
	var dots []wire.Dot
	if root.find(target, targetAddr-rootAddr, &dots) {
		return dots
	}
	Failf("traverse failed for root type %v @%x and target type %v @%x", root.typ, rootAddr, target.typ, targetAddr)
	panic("unreachable")
}

// encodeMap encodes a map.
func (es *encodeState) encodeMap(source value, dest *wire.Object) {
	length, nilMap := source.ops.mapOps.length(source)
	if nilMap {
		// Because there is a difference between a nil map and an empty
		// map, we need to not decode in the case of a truly nil map.
		*dest = wire.Nil{}
		return
	}
	l := length
	m := &wire.Map{
		Keys:   make([]wire.Object, l),
		Values: make([]wire.Object, l),
	}
	*dest = m
	i := 0
	source.ops.mapOps.visit(source, func(key, element value) {
		es.encodeValue(key, encodeAsValue, &m.Keys[i])
		es.encodeValue(element, encodeAsValue, &m.Values[i])
		i++
	})
}

// objectEncoder is for encoding structs.
type objectEncoder struct {
	// es is encodeState.
	es *encodeState

	// encoded is the encoded struct.
	encoded *wire.Record
}

// save is called by the public methods on Sink.
func (oe *objectEncoder) save(slot int, obj reflect.Value) {
	fieldValue := oe.encoded.Field(slot)
	oe.es.encodeObject(obj, encodeDefault, fieldValue)
}

// emptyStructRecord is immutable and has no generated saver or references.
var emptyStructRecord = wire.NewSlotRecord(0, 0)

// encodeStruct encodes a composite object.
func (es *encodeState) encodeStruct(source value, dest *wire.Object) {
	obj := source.reflect()
	if s, ok := es.encodedStructs[obj]; ok {
		*dest = s
		return
	}
	key := obj

	// Ensure that the obj is addressable. There are two cases when it is
	// not. First, is when this is dispatched via SaveValue. Second, when
	// this is a map key as a struct. Either way, we need to make a copy to
	// obtain an addressable value.
	if source.address == nil {
		obj = copyValue(obj)
		source.address = obj.Addr().UnsafePointer()
	} else if !obj.CanAddr() {
		obj = reflect.NewAt(source.ops.layout.typ, source.address).Elem()
	}

	// Look the type up in the database.
	te, ok := es.types.Lookup(obj.Type())
	if te == nil {
		if obj.NumField() == 0 {
			// Allow unregistered anonymous, empty structs. This
			// will just return success without ever invoking the
			// passed function. The shared empty record is never changed.
			//
			// Note that this mechanism does *not* work for
			// interfaces in general. So you can't dispatch
			// non-registered empty structs via interfaces because
			// then they can't be restored.
			*dest = emptyStructRecord
			es.encodedStructs[key] = emptyStructRecord
			return
		}
		// We need a SaverLoader for struct types.
		Failf("struct %T does not implement SaverLoader", obj.Interface())
	}
	if !ok {
		// Queue the type to be serialized.
		es.pendingTypes = append(es.pendingTypes, te.Type)
	}

	// Invoke the provided saver.
	s := te.newRecord(wire.TypeID(te.ID), len(te.Fields))
	*dest = s
	es.encodedStructs[key] = s
	oe := objectEncoder{
		es:      es,
		encoded: s,
	}
	es.stats.start(te.ID)
	defer es.stats.done()
	switch value := obj.Addr().Interface().(type) {
	case SaverLoader:
		value.StateSave(Sink{internal: oe})
	case binaryObject:
		es.saveBinary(value, te.Name, Sink{internal: oe})
	}
}

// encodeArray encodes an array.
func (es *encodeState) encodeArray(obj value, dest *wire.Object) {
	if captured, ok := es.captureArray(obj); ok {
		*dest = captured
		return
	}
	l := int(obj.ops.layout.length)
	a := &wire.Array{
		Contents: make([]wire.Object, l),
	}
	*dest = a
	for i := 0; i < l; i++ {
		// We need to encode the full value because arrays are encoded
		// using the type information from only the first element.
		es.encodeValue(obj.index(i), encodeAsValue, &a.Contents[i])
	}
}

// findType recursively finds type information.
func (es *encodeState) findType(typ reflect.Type) wire.TypeSpec {
	// First: check if this is a proper type. It's possible for pointers,
	// slices, arrays, maps, etc to all have some different type.
	te, ok := es.types.Lookup(typ)
	if te != nil {
		if !ok {
			// See encodeStruct.
			es.pendingTypes = append(es.pendingTypes, te.Type)
		}
		return wire.TypeID(te.ID)
	}

	switch typ.Kind() {
	case reflect.Ptr:
		return &wire.TypeSpecPointer{
			Type: es.findType(typ.Elem()),
		}
	case reflect.Slice:
		return &wire.TypeSpecSlice{
			Type: es.findType(typ.Elem()),
		}
	case reflect.Array:
		return &wire.TypeSpecArray{
			Count: wire.Uint(typ.Len()),
			Type:  es.findType(typ.Elem()),
		}
	case reflect.Map:
		return &wire.TypeSpecMap{
			Key:   es.findType(typ.Key()),
			Value: es.findType(typ.Elem()),
		}
	default:
		// After potentially chasing many pointers, the
		// ultimate type of the object is not known.
		Failf("type %q is not known", typ)
	}
	panic("unreachable")
}

// encodeInterface encodes an interface.
func (es *encodeState) encodeInterface(obj reflect.Value, dest *wire.Object) {
	// Dereference the object.
	obj = obj.Elem()
	if !obj.IsValid() {
		// Special case: the nil object.
		*dest = &wire.Interface{
			Type:  wire.TypeSpecNil{},
			Value: wire.Nil{},
		}
		return
	}

	// Encode underlying object.
	i := &wire.Interface{
		Type: es.findType(obj.Type()),
	}
	*dest = i
	es.encodeObject(obj, encodeAsValue, &i.Value)
}

// encodeStrategy is the strategy used for encodeObject.
type encodeStrategy int

const (
	// encodeDefault means types are encoded normally as references.
	encodeDefault encodeStrategy = iota

	// encodeAsValue means that types will never take short-circuited and
	// will always be encoded as a normal value.
	encodeAsValue

	// encodeMapAsValue means that even maps will be fully encoded.
	encodeMapAsValue
)

// encodeObject encodes an object.
func (es *encodeState) encodeObject(obj reflect.Value, how encodeStrategy, dest *wire.Object) {
	es.encodeValue(reflectedValue(obj), how, dest)
}

func (es *encodeState) encodeValue(obj value, how encodeStrategy, dest *wire.Object) {
	if how == encodeDefault && obj.ops.zeroUnsupported != nil {
		Failf("unknown type %q", obj.ops.zeroUnsupported.Name())
	}
	if how == encodeDefault && obj.ops.primitiveZero && obj.zero() {
		*dest = wire.Nil{}
		return
	}
	switch obj.ops.layout.kind {
	case reflect.Ptr: // Fast path: first.
		r := new(wire.Ref)
		*dest = r
		if obj.pointer() == nil {
			// May be in an array or elsewhere such that a value is
			// required. So we encode as a reference to the zero
			// object, which does not exist. Note that this has to
			// be handled correctly in the decode path as well.
			return
		}
		es.resolveValue(value{ops: obj.ops.elem, address: obj.pointer()}, uintptr(obj.pointer()), r, encodeDefault)
	case reflect.Bool:
		*dest = wire.Bool(obj.boolean())
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		*dest = wire.Int(obj.signed())
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64, reflect.Uintptr:
		*dest = wire.Uint(obj.unsigned())
	case reflect.Float32:
		*dest = wire.Float32(obj.floating())
	case reflect.Float64:
		*dest = wire.Float64(obj.floating())
	case reflect.Complex64:
		c := wire.Complex64(obj.complex())
		*dest = &c // Needs alloc.
	case reflect.Complex128:
		c := wire.Complex128(obj.complex())
		*dest = &c // Needs alloc.
	case reflect.String:
		s := wire.String(obj.text())
		*dest = &s // Needs alloc.
	case reflect.Array:
		es.encodeArray(obj, dest)
	case reflect.Slice:
		s := new(wire.Slice)
		*dest = s
		obj.ops.slice.capture(es, obj, s)
	case reflect.Interface:
		es.encodeInterface(obj.reflect(), dest)
	case reflect.Struct:
		es.encodeStruct(obj, dest)
	case reflect.Map:
		if how == encodeMapAsValue {
			es.encodeMap(obj, dest)
			return
		}
		r := new(wire.Ref)
		*dest = r
		es.resolveMapValue(obj, r)
	default:
		Failf("unknown object %#v", obj.reflect().Interface())
		panic("unreachable")
	}
}

// Save serializes the object graph rooted at obj.
func (es *encodeState) Save(obj reflect.Value) {
	es.stats.init()
	defer es.stats.fini(func(id typeID) string {
		return es.pendingTypes[id-1].Name
	})

	// Resolve the first object, which should queue a pile of additional
	// objects on the pending list. All queued objects should be fully
	// resolved, and we should be able to serialize after this call.
	var root wire.Ref
	source := reflectedValue(obj)
	es.resolveValue(source, uintptr(source.address), &root, encodeDefault)

	// Encode the graph.
	var oes *objectEncodeState
	if err := safely(func() {
		for oes = es.deferred.Front(); oes != nil; oes = es.deferred.Front() {
			// Remove and encode the object. Note that as a result
			// of this encoding, the object may be enqueued on the
			// deferred list yet again. That's expected, and why it
			// is removed first.
			es.deferred.Remove(oes)
			es.encodeValue(oes.obj.value(), oes.how, &oes.encoded)
		}
	}); err != nil {
		// Include the object in the error message, if available.
		if oes != nil && oes.obj.ops != nil {
			Failf("encoding error: %w\nfor object %#v", err, oes.obj.reflect().Interface())
		}
		Failf("encoding error: %w", err)
	}

	// Check that we have objects to serialize.
	if len(es.pending) == 0 {
		Failf("pending is empty?")
	}

	// Write the header with the number of objects.
	if err := WriteHeader(&es.w, uint64(len(es.pending)), true); err != nil {
		Failf("error writing header: %w", err)
	}

	// Serialize all pending types and pending objects. Note that we don't
	// bother removing from this list as we walk it because that just
	// wastes time. It will not change after this point.
	if err := safely(func() {
		for _, wt := range es.pendingTypes {
			// Encode the type.
			wire.Save(&es.w, &wt)
		}
		// Emit objects in ID order.
		ids := make([]objectID, 0, len(es.pending))
		for id := range es.pending {
			ids = append(ids, id)
		}
		sort.Slice(ids, func(i, j int) bool {
			return ids[i] < ids[j]
		})
		for _, id := range ids {
			// Encode the id.
			oes = nil
			wire.Save(&es.w, wire.Uint(id))
			// Marshal the object.
			oes = es.pending[id]
			wire.Save(&es.w, oes.encoded)
		}
	}); err != nil {
		if oes != nil {
			// Include the object and the error.
			Failf("error serializing object %#v: %w", oes.encoded, err)
		} else {
			Failf("error serializing type or ID: %w", err)
		}
	}
}

// objectFlag indicates that the length is a # of objects, rather than a raw
// byte length. When this is set on a length header in the stream, it may be
// decoded appropriately.
const objectFlag uint64 = 1 << 63

// WriteHeader writes a header.
//
// Each object written to the statefile should be prefixed with a header. In
// order to generate statefiles that play nicely with debugging tools, raw
// writes should be prefixed with a header with object set to false and the
// appropriate length. This will allow tools to skip these regions.
func WriteHeader(w *wire.Writer, length uint64, object bool) error {
	// Sanity check the length.
	if length&objectFlag != 0 {
		Failf("impossibly huge length: %d", length)
	}
	if object {
		length |= objectFlag
	}

	// Write a header.
	return safely(func() {
		wire.SaveUint(w, length)
	})
}

// addrSetFunctions is used by addrSet.
type addrSetFunctions struct{}

func (addrSetFunctions) MinKey() uintptr {
	return 0
}

func (addrSetFunctions) MaxKey() uintptr {
	return ^uintptr(0)
}

func (addrSetFunctions) ClearValue(val **objectEncodeState) {
	*val = nil
}

func (addrSetFunctions) Merge(r1 addrRange, val1 *objectEncodeState, r2 addrRange, val2 *objectEncodeState) (*objectEncodeState, bool) {
	if val1.obj == val2.obj {
		// This, should never happen. It would indicate that the same
		// object exists in two non-contiguous address ranges. Note
		// that this assertion can only be triggered if the race
		// detector is enabled.
		Failf("unexpected merge in addrSet @ %v and %v: %#v and %#v", r1, r2, val1.obj, val2.obj)
	}
	// Reject the merge.
	return val1, false
}

func (addrSetFunctions) Split(r addrRange, val *objectEncodeState, _ uintptr) (*objectEncodeState, *objectEncodeState) {
	// A split should never happen: we don't remove ranges.
	Failf("unexpected split in addrSet @ %v: %#v", r, val.obj)
	panic("unreachable")
}
