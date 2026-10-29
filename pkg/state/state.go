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

// Package state provides functionality related to saving and loading object
// graphs.  For most types, it provides a set of default saving / loading logic
// that will be invoked automatically if custom logic is not defined.
//
//	Kind             Support
//	----             -------
//	Bool             default
//	Int              default
//	Int8             default
//	Int16            default
//	Int32            default
//	Int64            default
//	Uint             default
//	Uint8            default
//	Uint16           default
//	Uint32           default
//	Uint64           default
//	Float32          default
//	Float64          default
//	Complex64        default
//	Complex128       default
//	Array            default
//	Chan             custom
//	Func             custom
//	Interface        default
//	Map              default
//	Ptr              default
//	Slice            default
//	String           default
//	Struct           custom (*) Unless zero-sized.
//	UnsafePointer    custom
//
// See README.md for an overview of how encoding and decoding works.
package state

import (
	"context"
	"fmt"
	"io"
	"reflect"
	"runtime"

	"gvisor.dev/gvisor/pkg/gohacks"
	"gvisor.dev/gvisor/pkg/state/wire"
)

// ErrState is returned when an error is encountered during encode/decode.
type ErrState struct {
	// err is the underlying error.
	err error

	// trace is the stack trace.
	trace string
}

// Error returns a sensible description of the state error.
func (e *ErrState) Error() string {
	return fmt.Sprintf("%v:\n%s", e.err, e.trace)
}

// Unwrap implements standard unwrapping.
func (e *ErrState) Unwrap() error {
	return e.err
}

// Save saves the object graph rooted at the value pointed to by rootPtr.
//
// The resulting stream can only be loaded by the same binary (more precisely,
// by a binary with the same registered types).
func Save(ctx context.Context, w io.Writer, rootPtr any) (Stats, error) {
	start := gohacks.Nanotime()
	e := efaceOf(&rootPtr)
	var ti *typeInfo
	if err := safely(func() {
		ti = typeInfoForRtype(e.typ)
		if ti.kind != reflect.Pointer || e.data == nil {
			Failf("Save requires a non-nil pointer, got %T", rootPtr)
		}
	}); err != nil {
		return Stats{}, err
	}
	es := newEncodeState(ctx, ti.elem)
	defer es.release()
	es.stats.enabled = collectStats.Load()
	out := &es.final
	err := safely(func() {
		if es.stats.enabled {
			es.stats.init()
			defer es.stats.fini()
		}
		es.save(e.data, ti.elem, out)
	})
	es.stats.bytes = len(out.b)
	es.stats.elapsed = gohacks.Nanotime() - start
	if err != nil {
		return es.stats, err
	}
	ww := wire.Writer{Writer: w}
	if err := WriteHeader(&ww, uint64(len(out.b)), true); err != nil {
		return es.stats, err
	}
	if _, err := w.Write(out.b); err != nil {
		return es.stats, err
	}
	return es.stats, nil
}

// Load loads the object graph saved by Save into the value pointed to by
// rootPtr. It reads exactly the bytes written by Save.
func Load(ctx context.Context, r io.Reader, rootPtr any) (Stats, error) {
	ds := newDecodeState(ctx)
	defer ds.release()
	ds.stats.enabled = collectStats.Load()
	start := gohacks.Nanotime()
	length, object, err := ReadHeader(&wire.Reader{Reader: r})
	if err != nil {
		return ds.stats, err
	}
	if !object {
		return ds.stats, fmt.Errorf("expected an object graph, got %d bytes of raw data", length)
	}
	if length > maxStreamLength {
		return ds.stats, fmt.Errorf("invalid object graph length %d", length)
	}
	data := make([]byte, length)
	if _, err := io.ReadFull(r, data); err != nil {
		return ds.stats, err
	}
	ds.in = decBuf{b: data}
	err = safely(func() {
		e := efaceOf(&rootPtr)
		ti := typeInfoForRtype(e.typ)
		if ti.kind != reflect.Pointer || e.data == nil {
			Failf("Load requires a non-nil pointer, got %T", rootPtr)
		}
		if ds.stats.enabled {
			ds.stats.init()
			defer ds.stats.fini()
		}
		ds.load(e.data, studyUse(ti.elem, useElement))
	})
	ds.stats.bytes = len(data)
	ds.stats.objects = len(ds.objs)
	ds.stats.elapsed = gohacks.Nanotime() - start
	return ds.stats, err
}

// maxStreamLength is the maximum length of an object graph.
const maxStreamLength = 1 << 40

// Sink is used for Type.StateSave.
type Sink struct {
	internal objectEncoder
}

// objectEncoder encodes the fields of a struct.
type objectEncoder struct {
	es *encodeState
	// st is the type of the struct.
	st *typeInfo
}

// Save adds the given object to the map.
//
// You should pass always pointers to the object you are saving. For example:
//
//	type X struct {
//		A int
//		B *int
//	}
//
//	func (x *X) StateTypeInfo(m Sink) state.TypeInfo {
//		return state.TypeInfo{
//			Name:   "pkg.X",
//			Fields: []string{
//				"A",
//				"B",
//			},
//		}
//	}
//
//	func (x *X) StateSave(m Sink) {
//		m.Save(0, &x.A) // Field is A.
//		m.Save(1, &x.B) // Field is B.
//	}
//
//	func (x *X) StateLoad(m Source) {
//		m.Load(0, &x.A) // Field is A.
//		m.Load(1, &x.B) // Field is B.
//	}
//
// Save and Load calls must be made in the same order.
func (s Sink) Save(slot int, objPtr any) {
	e := efaceOf(&objPtr)
	ti := s.internal.st.slotType(slot, e.typ, true)
	s.internal.es.encodeValue(ti, e.data)
}

// SaveValue adds the given object value to the map.
//
// This should be used for values where pointers are not available, or casts
// are required during Save/Load.
//
// For example, if we want to cast external package type P.Foo to int64:
//
//	func (x *X) StateSave(m Sink) {
//		m.SaveValue(0, "A", int64(x.A))
//	}
//
//	func (x *X) StateLoad(m Source) {
//		m.LoadValue(0, new(int64), func(x any) {
//			x.A = P.Foo(x.(int64))
//		})
//	}
func (s Sink) SaveValue(slot int, obj any) {
	e := efaceOf(&obj)
	ti := s.internal.st.slotType(slot, e.typ, false)
	s.internal.es.encodeDynamic(ti, e.data)
}

// Context returns the context object provided at save time.
func (s Sink) Context() context.Context {
	return s.internal.es.ctx
}

// Type describes a type's saved representation while minimizing runtime
// reflection. The package separately registers binary codecs for supported
// foreign structs.
//
// All these methods can be automatically generated by the go_stateify tool.
type Type interface {
	// StateTypeName returns the type's name.
	//
	// This is used for matching type information during encoding and
	// decoding, as well as dynamic interface dispatch. This should be
	// globally unique.
	StateTypeName() string

	// StateFields returns information about the type.
	//
	// Fields is the set of fields for the object. Calls to Sink.Save and
	// Source.Load must be made in-order with respect to these fields.
	//
	// The field list must be stable for the type.
	StateFields() []string
}

// SaverLoader defines a struct's state methods.
type SaverLoader interface {
	// StateSave saves the state of the object to the given Map.
	StateSave(Sink)

	// StateLoad loads the state of the object.
	StateLoad(context.Context, Source)
}

// Source is used for Type.StateLoad.
type Source struct {
	internal objectDecoder
}

// objectDecoder decodes the fields of a struct.
type objectDecoder struct {
	ds *decodeState
	// st is the type of the struct.
	st *typeInfo
}

// Load loads the given object passed as a pointer..
//
// See Sink.Save for an example.
func (s Source) Load(slot int, objPtr any) {
	e := efaceOf(&objPtr)
	ti := s.internal.st.slotType(slot, e.typ, true)
	s.internal.ds.decodeValue(ti, e.data)
}

// LoadWait loads the given objects from the map, and marks it as requiring all
// AfterLoad executions to complete prior to running this object's AfterLoad.
//
// See Sink.Save for an example.
func (s Source) LoadWait(slot int, objPtr any) {
	e := efaceOf(&objPtr)
	ti := s.internal.st.slotType(slot, e.typ, true)
	ds := s.internal.ds
	ds.lastRef = -1
	ds.decodeValue(ti, e.data)
	if ds.lastRef >= 0 {
		ds.wait(ds.cur, ds.lastRef, callback{waiter: -1})
	}
}

// LoadValue loads the given object value from the map.
//
// See Sink.SaveValue for an example.
func (s Source) LoadValue(slot int, objPtr any, fn func(any)) {
	e := efaceOf(&objPtr)
	ti := s.internal.st.slotType(slot, e.typ, true)
	ds := s.internal.ds
	ds.lastRef = -1
	keepZero := ds.keepZero
	ds.keepZero = false
	ds.decodeValue(ti, e.data)
	ds.keepZero = keepZero
	cb := callback{valueFn: fn, value: makeAny(ti, e.data), waiter: -1}
	if ds.lastRef >= 0 {
		ds.wait(ds.cur, ds.lastRef, cb)
	} else {
		ds.runCallback(&cb)
	}
}

// ValueLoader is implemented by the generated code of structs with fields
// saved through values (see Source.LoadValueTo).
type ValueLoader interface {
	// StateLoadValue loads the value of the field in the given slot.
	StateLoadValue(ctx context.Context, slot int, value any)
}

// AfterLoader is implemented by the generated code of structs with afterLoad
// methods (see Source.AfterLoadTo).
type AfterLoader interface {
	// StateAfterLoad calls the afterLoad method.
	StateAfterLoad(ctx context.Context)
}

// LoadValueTo is like LoadValue, but calls l.StateLoadValue(ctx, slot, value)
// instead of a function. typ is a nil pointer to the type of the value.
// StateLoadValue must not retain the address of the value, which it must
// copy.
//
// This is used by go_stateify, and avoids the allocations of LoadValue.
func (s Source) LoadValueTo(slot int, typ any, l ValueLoader) {
	e := efaceOf(&typ)
	ti := s.internal.st.slotType(slot, e.typ, true)
	s.internal.ds.loadValue(ti, slot, l)
}

// AfterLoadTo is like AfterLoad, but calls l.StateAfterLoad.
//
// This is used by go_stateify, and avoids the allocation of a function.
func (s Source) AfterLoadTo(l AfterLoader) {
	ds := s.internal.ds
	if ds.noCallbacks {
		return
	}
	ds.addCallback(ds.cur, callback{afterLoader: l, waiter: -1})
}

// AfterLoad schedules a function execution when all objects have been
// allocated and their automated loading and customized load logic have been
// executed. fn will not be executed until all of current object's
// dependencies' AfterLoad() logic, if exist, have been executed.
func (s Source) AfterLoad(fn func()) {
	ds := s.internal.ds
	if ds.noCallbacks {
		return
	}
	ds.addCallback(ds.cur, callback{fn: fn, waiter: -1})
}

// Context returns the context object provided at load time.
func (s Source) Context() context.Context {
	return s.internal.ds.ctx
}

// IsZeroValue checks if the given value is the zero value.
//
// This function is used by the stateify tool.
func IsZeroValue(val any) bool {
	return val == nil || reflect.ValueOf(val).Elem().IsZero()
}

// Failf is a wrapper around panic that should be used to generate errors that
// can be caught during saving and loading.
func Failf(fmtStr string, v ...any) {
	panic(fmt.Errorf(fmtStr, v...))
}

// safely executes the given function, catching a panic and unpacking as an
// error.
//
// The error flow through the state package uses panic and recover. There are
// two important reasons for this:
//
// 1) Many of the reflection methods will already panic with invalid data or
// violated assumptions. We would want to recover anyways here.
//
// 2) It allows us to eliminate boilerplate within Save() and Load() functions.
// In nearly all cases, when the low-level serialization functions fail, you
// will want the checkpoint to fail anyways. Plumbing errors through every
// method doesn't add a lot of value. If there are specific error conditions
// that you'd like to handle, you should add appropriate functionality to
// objects themselves prior to calling Save() and Load().
func safely(fn func()) (err error) {
	defer func() {
		if r := recover(); r != nil {
			if es, ok := r.(*ErrState); ok {
				err = es // Propagate.
				return
			}

			// Build a new state error.
			es := new(ErrState)
			if e, ok := r.(error); ok {
				es.err = e
			} else {
				es.err = fmt.Errorf("%v", r)
			}

			// Make a stack. We don't know how big it will be ahead
			// of time, but want to make sure we get the whole
			// thing. So we just do a stupid brute force approach.
			var stack []byte
			for sz := 1024; ; sz *= 2 {
				stack = make([]byte, sz)
				n := runtime.Stack(stack, false)
				if n < sz {
					es.trace = string(stack[:n])
					break
				}
			}

			// Set the error.
			err = es
		}
	}()

	// Execute the function.
	fn()
	return nil
}

// objectFlag indicates that the length is that of an object graph, rather
// than of raw data. When this is set on a length header in the stream, it may
// be decoded appropriately.
const objectFlag uint64 = 1 << 63

// WriteHeader writes a header.
//
// Each object graph written to the statefile is prefixed with a header. In
// order to generate statefiles that play nicely with debugging tools, raw
// writes should be prefixed with a header with object set to false and the
// appropriate length. This will allow tools to skip these regions.
func WriteHeader(w *wire.Writer, length uint64, object bool) error {
	// Sanity check the length.
	if length&objectFlag != 0 {
		return fmt.Errorf("impossibly huge length: %d", length)
	}
	if object {
		length |= objectFlag
	}
	return safely(func() {
		wire.SaveUint(w, length)
	})
}

// ReadHeader reads a header.
//
// Each object graph written to the statefile is prefixed with a header. See
// WriteHeader for more information; these functions are exported to allow
// non-state writes to the file to play nice with debugging tools.
func ReadHeader(r *wire.Reader) (length uint64, object bool, err error) {
	err = safely(func() {
		length = wire.LoadUint(r)
	})
	if err != nil {
		// On the header, pass raw I/O errors.
		if sErr, ok := err.(*ErrState); ok {
			return 0, false, sErr.Unwrap()
		}
		return 0, false, err
	}
	object = length&objectFlag != 0
	length &^= objectFlag
	return length, object, nil
}
