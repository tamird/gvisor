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
	"hash/fnv"
	"reflect"
	"sync"
	"sync/atomic"
	"unsafe"
)

// structKind classifies struct types.
type structKind uint8

const (
	// structUnsupported structs can't be saved.
	structUnsupported structKind = iota
	// structEmpty structs have no saved state.
	structEmpty
	// structSaverLoader structs implement SaverLoader.
	structSaverLoader
	// structBinary structs implement binaryObject and are registered with
	// registerBinary.
	structBinary
)

// typeInfo describes a Go type. It is built once per type, using reflection,
// so that values can be saved and loaded without reflection.
//
// typeInfos are immutable once published, except for slots.
type typeInfo struct {
	// id is a unique small integer, used to index per-stream tables.
	id int32

	typ   reflect.Type
	rtype unsafe.Pointer
	kind  reflect.Kind
	size  uintptr

	// elem is the element type of pointers, slices, arrays and maps.
	elem *typeInfo
	// key is the key type of maps.
	key *typeInfo
	// length is the length of arrays.
	length uintptr

	// direct is true if values of this type are stored directly in the data
	// word of interfaces, rather than pointed to by it.
	direct bool

	// pod is true if values of this type are saved as their memory
	// representation.
	pod bool

	// inlineStruct is true if values of this type contain (not through
	// pointers) structs with StateSave methods, which must be called once per
	// value.
	inlineStruct bool

	// emptyIface is true for interfaces with no methods.
	emptyIface bool

	// needsExact is true if values of this type contain (not through
	// pointers) non-empty interfaces or maps, which can't be decoded using a
	// type with only the same layout.
	needsExact bool

	// partial is true if decoding values of this type may not write all of
	// their memory, such as fields that are not saved.
	partial bool

	// hooks is true for structs whose StateLoad methods may call code other
	// than generated code, such as hand-written StateLoad methods.
	hooks bool

	// structKind classifies struct types.
	structKind structKind

	// methodItab is the itab of StructType (structSaverLoader) or
	// binaryObject (structBinary) for pointers to this type.
	methodItab unsafe.Pointer

	// name is the registered name of the type, if any.
	name string

	// registered is true if the type is registered with its name.
	registered bool

	// sliceType is the type of slices of this type, once needed.
	sliceType atomic.Pointer[reflect.Type]

	// hintBytes and hintTargets are the length and number of targets of the
	// last graph with this root type.
	hintBytes, hintTargets atomic.Int32

	// slots caches the types of the fields saved and loaded by the
	// SaverLoader methods of structs, by slot.
	slots []atomic.Pointer[slotEntry]
}

// slotEntry is the type of a struct field.
type slotEntry struct {
	// rtype is the runtime type passed to Sink and Source methods: a pointer
	// to the field, or the field's value.
	rtype unsafe.Pointer
	// ti is the type of the field.
	ti *typeInfo
}

var (
	// typeInfosMu protects the variables below, and serializes building
	// typeInfos. Lookups are mostly done through per-struct slot caches and
	// per-stream caches.
	typeInfosMu sync.RWMutex

	// typeInfos maps runtime type descriptors to typeInfos.
	typeInfos map[unsafe.Pointer]*typeInfo

	// typeInfoCount is the number of typeInfos.
	typeInfoCount int32

	// typeInfoSlab and slotEntrySlab hold storage for new typeInfos and
	// slotEntries, which are never freed, to reduce the number of
	// allocations.
	typeInfoSlab  []typeInfo
	slotEntrySlab []slotEntry
	slotsSlab     []atomic.Pointer[slotEntry]
)

// typeInfoSlabSize is the number of typeInfos and slotEntries per allocation.
const typeInfoSlabSize = 64

// lookupTypeInfo returns the typeInfo for a runtime type descriptor, or nil.
func lookupTypeInfo(rt unsafe.Pointer) *typeInfo {
	typeInfosMu.RLock()
	ti := typeInfos[rt]
	typeInfosMu.RUnlock()
	return ti
}

// typeInfoForRtype returns the typeInfo for a runtime type descriptor.
func typeInfoForRtype(rt unsafe.Pointer) *typeInfo {
	return studyUse(typeInfoFromRtype(rt), useLookup)
}

func typeInfoFromRtype(rt unsafe.Pointer) *typeInfo {
	if ti := lookupTypeInfo(rt); ti != nil {
		return ti
	}
	return buildTypeInfo(typeOfRtype(rt))
}

// typeInfoOf returns the typeInfo for t.
func typeInfoOf(t reflect.Type) *typeInfo {
	if ti := lookupTypeInfo(rtypeOf(t)); ti != nil {
		return studyUse(ti, useLookup)
	}
	return studyUse(buildTypeInfo(t), useLookup)
}

// newSlots returns n slot cache entries.
//
// Preconditions: typeInfosMu is locked.
func newSlots(n int) []atomic.Pointer[slotEntry] {
	if n > typeInfoSlabSize {
		return make([]atomic.Pointer[slotEntry], n)
	}
	if len(slotsSlab) < n {
		slotsSlab = make([]atomic.Pointer[slotEntry], 4*typeInfoSlabSize)
	}
	slots := slotsSlab[:n:n]
	slotsSlab = slotsSlab[n:]
	return slots
}

// newSlotEntry returns a new slotEntry.
func newSlotEntry(rt unsafe.Pointer, ti *typeInfo) *slotEntry {
	typeInfosMu.Lock()
	defer typeInfosMu.Unlock()
	if len(slotEntrySlab) == 0 {
		slotEntrySlab = make([]slotEntry, typeInfoSlabSize)
	}
	e := &slotEntrySlab[0]
	slotEntrySlab = slotEntrySlab[1:]
	*e = slotEntry{rtype: rt, ti: ti}
	return e
}

// sliceTypeOf returns the type of slices of st.
func (st *typeInfo) sliceTypeOf() reflect.Type {
	if t := st.sliceType.Load(); t != nil {
		return *t
	}
	t := reflect.SliceOf(st.typ)
	st.sliceType.Store(&t)
	return t
}

// typeInfoBuilder builds typeInfos, which are published once complete.
//
// Preconditions: typeInfosMu is locked.
type typeInfoBuilder struct {
	// pending are the typeInfos being built.
	pending []*typeInfo
}

func buildTypeInfo(t reflect.Type) *typeInfo {
	typeInfosMu.Lock()
	defer typeInfosMu.Unlock()
	var pending [8]*typeInfo
	b := typeInfoBuilder{pending: pending[:0]}
	ti := b.get(t)
	if typeInfos == nil {
		typeInfos = make(map[unsafe.Pointer]*typeInfo)
	}
	studyBuilt(b.pending, ti)
	for _, p := range b.pending {
		typeInfos[p.rtype] = p
	}
	return ti
}

// isDirect returns true if values of t are stored directly in interfaces.
func isDirect(t reflect.Type) bool {
	if t.Kind() == reflect.Interface || t.Size() != unsafe.Sizeof(uintptr(0)) {
		// Only pointer-shaped values are direct. This avoids allocating
		// large values below.
		return false
	}
	v := reflect.Zero(t).Interface()
	// Values stored indirectly are pointed to by a non-nil data word, even
	// for zero values. The data word holds zero values of direct types.
	return efaceOf(&v).data == nil
}

func (b *typeInfoBuilder) get(t reflect.Type) *typeInfo {
	rtype := rtypeOf(t)
	if ti := typeInfos[rtype]; ti != nil {
		return ti
	}
	for _, ti := range b.pending {
		if ti.rtype == rtype {
			return ti
		}
	}
	if len(typeInfoSlab) == 0 {
		typeInfoSlab = make([]typeInfo, typeInfoSlabSize)
	}
	ti := &typeInfoSlab[0]
	typeInfoSlab = typeInfoSlab[1:]
	ti.id = typeInfoCount
	ti.typ = t
	ti.rtype = rtype
	ti.kind = t.Kind()
	ti.size = t.Size()
	typeInfoCount++
	b.pending = append(b.pending, ti)
	ti.direct = isDirect(t)
	// Only defined types and structs (with embedded fields) have methods.
	// The itabs of StructType for registered structs, and of Type for other
	// registered types, are static (see RegisterStruct), so these type
	// assertions do not build itabs.
	var (
		pt     reflect.Type
		st     StructType
		isType bool
	)
	if ti.kind == reflect.Struct {
		pt = reflect.PointerTo(t)
		st, _ = reflect.Zero(pt).Interface().(StructType)
	}
	if st != nil {
		ti.name = st.StateTypeName()
		isType = true
	} else if t.Name() != "" || ti.kind == reflect.Struct {
		pt = reflect.PointerTo(t)
		if tt, ok := reflect.Zero(pt).Interface().(Type); ok {
			ti.name = tt.StateTypeName()
			isType = true
		}
	}
	if info, ok := binaryTypes[t]; ok {
		ti.name = info
	}
	rt := globalTypeDatabase[ti.name]
	ti.registered = ti.name != "" && rt.typ == t
	switch ti.kind {
	case reflect.Bool, reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64,
		reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64, reflect.Uintptr,
		reflect.Float32, reflect.Float64, reflect.Complex64, reflect.Complex128:
		ti.pod = true
	case reflect.Pointer, reflect.Slice:
		ti.elem = b.get(t.Elem())
	case reflect.Array:
		ti.elem = b.get(t.Elem())
		ti.length = uintptr(t.Len())
		studyUse(ti.elem, useArrayFlags)
		ti.pod = ti.elem.pod
		ti.inlineStruct = ti.length > 0 && ti.elem.inlineStruct
		ti.needsExact = ti.elem.needsExact
		ti.partial = ti.elem.partial
	case reflect.Map:
		ti.key = b.get(t.Key())
		ti.elem = b.get(t.Elem())
		ti.needsExact = true
	case reflect.Interface:
		ti.emptyIface = t.NumMethod() == 0
		ti.needsExact = !ti.emptyIface
	case reflect.Struct:
		switch {
		case st != nil:
			ti.structKind = structSaverLoader
			ti.inlineStruct = true
			ti.methodItab = (*iface)(unsafe.Pointer(&st)).tab
			fields := st.StateFields()
			if !eagerTypeInfo {
				// Register did not validate the type.
				validateTypeInfo(ti.name, fields)
			}
			ti.slots = newSlots(len(fields))
			ti.hooks = !ti.registered || !rt.noHooks
			if !ti.hooks && b.plainFields(t) {
				// The StateSave and StateLoad methods only save and load
				// the fields, which are all plain data.
				ti.pod = true
				ti.inlineStruct = false
			}
		case binaryTypes[t] != "":
			ti.structKind = structBinary
			bo := reflect.Zero(pt).Interface().(binaryObject)
			ti.methodItab = (*iface)(unsafe.Pointer(&bo)).tab
		case t.NumField() == 0 || isType:
			// Unregistered empty structs, and structs that implement only
			// Type, have no saved state.
			ti.structKind = structEmpty
			ti.pod = t.NumField() == 0
		}
		ti.partial = !ti.pod
	}
	return ti
}

// plainFields returns true if all fields of the struct t are saved plain data,
// so that the memory representation of t can be saved.
func (b *typeInfoBuilder) plainFields(t reflect.Type) bool {
	for i := 0; i < t.NumField(); i++ {
		f := t.Field(i)
		if f.Name != "_" {
			switch f.Tag.Get("state") {
			case "", "wait":
			default:
				return false
			}
		}
		if f.Type.Kind() == reflect.Struct && f.Type.Name() == "" {
			// Fields of anonymous structs are saved as fields of t.
			if !b.plainFields(f.Type) {
				return false
			}
			continue
		}
		switch f.Type.Kind() {
		case reflect.Bool, reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64,
			reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64, reflect.Uintptr,
			reflect.Float32, reflect.Float64, reflect.Complex64, reflect.Complex128:
			// Avoid building typeInfos for these.
			continue
		case reflect.Array, reflect.Struct:
		default:
			return false
		}
		if !studyUse(b.get(f.Type), usePlainField).pod {
			return false
		}
	}
	return true
}

// slotType returns the type of the field in the given slot of st, given the
// runtime type passed to a Sink or Source method. If ptr is true, rt is a
// pointer to the field.
func (st *typeInfo) slotType(slot int, rt unsafe.Pointer, ptr bool) *typeInfo {
	if uint(slot) < uint(len(st.slots)) {
		if e := st.slots[slot].Load(); e != nil && e.rtype == rt {
			return studyUse(e.ti, useSlotValue)
		}
	}
	if rt == nil {
		Failf("nil value saved in slot %d of %v", slot, st.typ)
	}
	ti := typeInfoFromRtype(rt)
	if ptr {
		studyUse(ti, useSlotPointer)
		if ti.kind != reflect.Pointer {
			Failf("non-pointer %v passed for slot %d of %v", ti.typ, slot, st.typ)
		}
		ti = ti.elem
	}
	if uint(slot) < uint(len(st.slots)) {
		st.slots[slot].Store(newSlotEntry(rt, ti))
	}
	return studyUse(ti, useSlotValue)
}

// typeCacheSize is the number of entries of typeCache and itabCache.
const typeCacheSize = 256

// typeCache caches the typeInfos of runtime type descriptors and of the
// dynamic types of itabs, avoiding lookups in global maps. Keys are never
// freed, since they are part of the binary or persistently allocated.
type typeCache [typeCacheSize]struct {
	key unsafe.Pointer
	ti  *typeInfo
}

func (c *typeCache) entry(key unsafe.Pointer) *struct {
	key unsafe.Pointer
	ti  *typeInfo
} {
	h := uintptr(key) >> 3
	h ^= h >> 8
	return &c[h%typeCacheSize]
}

// rtype returns the typeInfo of a runtime type descriptor.
func (c *typeCache) rtype(rt unsafe.Pointer) *typeInfo {
	e := c.entry(rt)
	if e.key != rt {
		e.key, e.ti = rt, typeInfoForRtype(rt)
	}
	return e.ti
}

// itab returns the dynamic type of the non-nil, non-empty interface value at
// p.
func (c *typeCache) itab(p unsafe.Pointer) *typeInfo {
	tab := (*iface)(p).tab
	e := c.entry(tab)
	if e.key != tab {
		e.key, e.ti = tab, itabType(p)
	}
	return e.ti
}

// itabCache caches itabs by interface and dynamic type, avoiding lookups in
// global maps.
type itabCache [typeCacheSize]struct {
	iface, dyn *typeInfo
	tab        unsafe.Pointer
}

// get returns the itab for values of type dyn in interfaces of type
// ifaceType.
func (c *itabCache) get(ifaceType, dyn *typeInfo) unsafe.Pointer {
	e := &c[uint32(ifaceType.id*31+dyn.id)%typeCacheSize]
	if e.iface != ifaceType || e.dyn != dyn {
		e.iface, e.dyn, e.tab = ifaceType, dyn, itabFor(ifaceType, dyn)
	}
	return e.tab
}

// itabType returns the dynamic type of the non-nil, non-empty interface value
// at p.
func itabType(p unsafe.Pointer) *typeInfo {
	// The dynamic type follows the interface type in itabs (abi.ITab).
	return typeInfoForRtype((*[2]unsafe.Pointer)((*iface)(p).tab)[1])
}

// itabKey identifies an itab.
type itabKey struct {
	iface, dyn *typeInfo
}

// itabs caches itabs by interface and dynamic type. Protected by
// typeInfosMu.
var itabs map[itabKey]unsafe.Pointer

// itabFor returns the itab for values of type dyn in interfaces of type
// ifaceType.
func itabFor(ifaceType, dyn *typeInfo) unsafe.Pointer {
	k := itabKey{ifaceType, dyn}
	typeInfosMu.RLock()
	tab, ok := itabs[k]
	typeInfosMu.RUnlock()
	if ok {
		return tab
	}
	// Set panics if dyn does not implement the interface.
	v := reflect.New(ifaceType.typ).Elem()
	v.Set(reflect.Zero(dyn.typ))
	tab = (*iface)(v.Addr().UnsafePointer()).tab
	typeInfosMu.Lock()
	if itabs == nil {
		itabs = make(map[itabKey]unsafe.Pointer)
	}
	itabs[k] = tab
	typeInfosMu.Unlock()
	return tab
}

// builtinTypes are the predeclared types, by kind.
var builtinTypes = [...]reflect.Type{
	reflect.Bool:       reflect.TypeFor[bool](),
	reflect.Int:        reflect.TypeFor[int](),
	reflect.Int8:       reflect.TypeFor[int8](),
	reflect.Int16:      reflect.TypeFor[int16](),
	reflect.Int32:      reflect.TypeFor[int32](),
	reflect.Int64:      reflect.TypeFor[int64](),
	reflect.Uint:       reflect.TypeFor[uint](),
	reflect.Uint8:      reflect.TypeFor[uint8](),
	reflect.Uint16:     reflect.TypeFor[uint16](),
	reflect.Uint32:     reflect.TypeFor[uint32](),
	reflect.Uint64:     reflect.TypeFor[uint64](),
	reflect.Uintptr:    reflect.TypeFor[uintptr](),
	reflect.Float32:    reflect.TypeFor[float32](),
	reflect.Float64:    reflect.TypeFor[float64](),
	reflect.Complex64:  reflect.TypeFor[complex64](),
	reflect.Complex128: reflect.TypeFor[complex128](),
	reflect.String:     reflect.TypeFor[string](),
}

var (
	anyType         = reflect.TypeFor[any]()
	emptyStructType = reflect.TypeFor[struct{}]()
)

// fingerprint is the cached schemaFingerprint, or zero.
var fingerprint atomic.Uint64

// schemaFingerprint returns a hash of the names, kinds, sizes and number of
// fields of all registered types. Streams can only be loaded by binaries with
// the same fingerprint.
func schemaFingerprint() uint64 {
	if f := fingerprint.Load(); f != 0 {
		return f
	}
	var f uint64
	h := fnv.New64a()
	var buf [8]byte
	for name, rt := range globalTypeDatabase {
		t := rt.typ
		h.Reset()
		h.Write([]byte(name))
		n := uint64(t.Size())<<16 | uint64(t.Kind())
		if t.Kind() == reflect.Struct {
			n |= uint64(t.NumField()) << 48
		}
		for i := range buf {
			buf[i] = byte(n >> (8 * i))
		}
		h.Write(buf[:])
		// Combine independently of map iteration order.
		f += h.Sum64()
	}
	f |= 1 // Never zero.
	fingerprint.Store(f)
	return f
}
