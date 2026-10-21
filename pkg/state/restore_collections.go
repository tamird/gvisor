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
	"reflect"

	"gvisor.dev/gvisor/pkg/state/wire"
)

// restoreMap owns temporary cells and Go's map insertion semantics. Aggregate
// cells remain distinct because deferred loaders can retain their addresses.
// Waits precede insertion, which reads the live map header.
func (ds *decodeState) restoreMap(ods *objectDecodeState, object reflect.Value, encoded *wire.Map) {
	restoreMapEntries(Decoder{ds: ds, ods: ods}, reflectedMap{object}, reflectedCell{}, reflectedCell{}, encoded)
}

type cellOperation[C any] interface {
	Restore(Decoder, C)
}

type mapStorage[K, V any] interface {
	ensure()
	key() K
	value() V
	zeroKey(K)
	zeroValue(V)
	reuseKey() bool
	reuseValue() bool
	insert(K, V)
}

func restoreMapEntries[K, V any, M mapStorage[K, V], KO cellOperation[K], VO cellOperation[V]](d Decoder, object M, keyOp KO, valueOp VO, encoded *wire.Map) {
	object.ensure()
	if len(encoded.Keys) == 0 {
		return
	}
	// Other kinds are copied into the map; interface values get independent
	// dynamic storage in decodeInterface.
	reuseKey, reuseValue := object.reuseKey(), object.reuseValue()
	var keyCell K
	var valueCell V
	for i, key := range encoded.Keys {
		value := encoded.Values[i]
		if i == 0 || !reuseKey {
			keyCell = object.key()
		} else {
			// Nil wire values leave the destination unchanged.
			object.zeroKey(keyCell)
		}
		if i == 0 || !reuseValue {
			valueCell = object.value()
		} else {
			object.zeroValue(valueCell)
		}
		keyOp.Restore(d.child(key), keyCell)
		valueOp.Restore(d.child(value), valueCell)
		d.ds.waitObject(d.ods, key, nil)
		d.ds.waitObject(d.ods, value, nil)
		object.insert(keyCell, valueCell)
	}
}

func reusableCell(typ reflect.Type) bool {
	return typ.Kind() != reflect.Struct && typ.Kind() != reflect.Array
}

type reflectedMap struct{ object reflect.Value }

func (m reflectedMap) ensure() {
	if m.object.IsNil() {
		m.object.Set(reflect.MakeMap(m.object.Type()))
	}
}
func (m reflectedMap) key() reflect.Value              { return reflect.New(m.object.Type().Key()).Elem() }
func (m reflectedMap) value() reflect.Value            { return reflect.New(m.object.Type().Elem()).Elem() }
func (reflectedMap) zeroKey(v reflect.Value)           { v.SetZero() }
func (reflectedMap) zeroValue(v reflect.Value)         { v.SetZero() }
func (m reflectedMap) reuseKey() bool                  { return reusableCell(m.object.Type().Key()) }
func (m reflectedMap) reuseValue() bool                { return reusableCell(m.object.Type().Elem()) }
func (m reflectedMap) insert(key, value reflect.Value) { m.object.SetMapIndex(key, value) }

type arrayStorage[C any] interface {
	length() int
	cell(int) C
	primitives(wire.Array)
}

// restoreArrayElements owns traversal and waits for both storage adapters.
// Cells are reacquired after each hook, so manual slice destinations retain
// their live-header semantics.
func restoreArrayElements[C any, A arrayStorage[C], O cellOperation[C]](d Decoder, target A, op O, encoded wire.Array) {
	length := target.length()
	if encoded.Len() != length {
		Failf("mismatching array length expect=%d, actual=%d", length, encoded.Len())
	}
	if length == 0 {
		return
	}
	switch values := encoded.(type) {
	case *wire.ObjectArray:
		for i, value := range values.Contents {
			op.Restore(d.child(value), target.cell(i))
			d.ds.waitObject(d.ods, value, nil)
		}
	case *wire.RefArray:
		for i := range values.Contents {
			value := &values.Contents[i]
			op.Restore(d.child(value), target.cell(i))
			d.ds.waitObject(d.ods, value, nil)
		}
	default:
		target.primitives(encoded)
	}
}

type reflectedArray struct{ target reflect.Value }

func (a reflectedArray) length() int              { return a.target.Len() }
func (a reflectedArray) cell(i int) reflect.Value { return a.target.Index(i) }
func (a reflectedArray) primitives(encoded wire.Array) {
	restorePrimitiveArray(a.target, encoded)
}
