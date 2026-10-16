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

package wire

import (
	"fmt"
	"unsafe"

	"gvisor.dev/gvisor/pkg/gohacks"
)

// Load parses one complete value into independently owned storage before
// returning it. It does not restore destinations or run callbacks while parsing.
// Returned subtrees remain valid after further reads, including failed reads.
//
// +checkescape:hard
func Load(r *Reader) Value {
	return loadPayload(r, Kind(loadUint(r)))
}

// loadPayload is the sole data-value grammar. Arrays and maps retain the
// existing omission of repeated tags after their first element.
func loadPayload(r *Reader, kind Kind) Value {
	switch kind {
	case KindBool:
		var bits uint64
		if loadBool(r) {
			bits = 1
		}
		return scalarValue(kind, bits)
	case KindInt:
		return scalarValue(kind, uint64(loadInt(r)))
	case KindUint, KindFloat64:
		return scalarValue(kind, uint64(loadUint(r)))
	case KindFloat32:
		return scalarValue(kind, uint64(uint32(loadUint(r))))
	case KindComplex64:
		re, im := uint32(loadUint(r)), uint32(loadUint(r))
		return scalarValue(kind, uint64(re)|uint64(im)<<32)
	case KindComplex128:
		re, im := uint64(loadUint(r)), uint64(loadUint(r))
		p := &complexValue{valueHeader: valueHeader{kind}, imaginary: im} // Escapes: owned complex payload.
		return Value{bits: re, payload: unsafe.Pointer(p)}
	case KindNil, KindNoObjects:
		return scalarValue(kind, 0)
	case KindRef:
		return loadReference(r)
	case KindString:
		value := string(loadString(r))
		p := &stringValue{valueHeader: valueHeader{kind}, value: value} // Escapes: owned string payload.
		return Value{payload: unsafe.Pointer(p)}
	case KindSlice:
		length, capacity := loadUint(r), loadUint(r)
		ref := loadReference(r)
		p := &sliceValue{valueHeader: valueHeader{kind}, capacity: capacity, ref: ref} // Escapes: owned slice payload.
		return Value{bits: uint64(length), payload: unsafe.Pointer(p)}
	case KindArray, KindMultipleObjects:
		count := loadUint(r)
		p := &arrayValue{valueHeader: valueHeader{kind}} // Escapes: owned collection payload.
		if count != 0 || kind == KindMultipleObjects {
			p.values = make([]Value, count) // Escapes: owned collection elements; make checks the full-width count.
			for i := range p.values {
				if i == 0 || kind == KindMultipleObjects {
					p.values[i] = Load(r)
				} else {
					p.values[i] = loadPayload(r, p.values[0].Kind())
				}
			}
		}
		return Value{payload: unsafe.Pointer(p)}
	case KindMap:
		count := loadUint(r)
		p := &mapValue{valueHeader: valueHeader{kind}} // Escapes: owned map payload.
		if count != 0 {
			p.entries = make([]valuePair, count) // Escapes: owned entries; make checks the full-width count.
			for i := range p.entries {
				if i == 0 {
					p.entries[i] = valuePair{key: Load(r), value: Load(r)}
				} else {
					p.entries[i] = valuePair{
						key:   loadPayload(r, p.entries[0].key.Kind()),
						value: loadPayload(r, p.entries[0].value.Kind()),
					}
				}
			}
		}
		return Value{payload: unsafe.Pointer(p)}
	case KindStruct:
		id := uint64(loadUint(r))
		fields := Load(r)
		p := &structValue{valueHeader: valueHeader{kind}, fields: fields} // Escapes: owned struct payload.
		return Value{bits: id, payload: unsafe.Pointer(p)}
	case KindInterface:
		typ, value := loadType(r), Load(r)
		p := &interfaceValue{valueHeader: valueHeader{kind}, typ: typ, value: value} // Escapes: owned interface payload.
		return Value{payload: unsafe.Pointer(p)}
	case KindType:
		p := &typeValue{valueHeader: valueHeader{kind}} // Escapes: owned schema payload.
		p.typ.Name = string(loadString(r))
		p.typ.Fields = make([]string, loadUint(r)) // Escapes: owned schema field headers.
		for i := range p.typ.Fields {
			p.typ.Fields[i] = string(loadString(r))
		}
		return Value{payload: unsafe.Pointer(p)}
	default:
		panic(fmt.Errorf("unknown header: %d", kind)) // Escapes: error formatting.
	}
}

func loadReference(r *Reader) Value {
	root := uint64(loadUint(r))
	count := loadUint(r)
	if count == 0 {
		return scalarValue(KindRef, root)
	}
	p := &refValue{valueHeader: valueHeader{KindRef}} // Escapes: owned reference payload.
	p.dots = make([]Dot, count)                       // Escapes: owned reference path; make checks the full-width count.
	for i := range p.dots {
		dot := loadInt(r)
		if dot >= 0 {
			p.dots[i] = Index(dot) // Escapes: existing Index narrowing and interface boxing.
		} else {
			data := make([]byte, -dot) // Escapes: independently owned field name.
			readFull(r, data)
			name := FieldName(gohacks.StringFromImmutableBytes(data)) // Escapes: owned field-name interface.
			p.dots[i] = &name
		}
	}
	p.typ = loadType(r)
	return Value{bits: root, payload: unsafe.Pointer(p)}
}

// loadType is the sole TypeSpec grammar. Its tag namespace is independent of
// data-value tags; full-width IDs and array counts remain intact.
func loadType(r *Reader) TypeSpec {
	header := loadUint(r)
	switch header {
	case typeSpecTypeID:
		return TypeID(loadUint(r)) // Escapes: owned type ID interface.
	case typeSpecPointer:
		return &TypeSpecPointer{Type: loadType(r)} // Escapes: owned type metadata.
	case typeSpecSlice:
		return &TypeSpecSlice{Type: loadType(r)} // Escapes: owned type metadata.
	case typeSpecArray:
		count := loadUint(r)
		return &TypeSpecArray{Count: count, Type: loadType(r)} // Escapes: owned type metadata.
	case typeSpecMap:
		key, value := loadType(r), loadType(r)
		return &TypeSpecMap{Key: key, Value: value} // Escapes: owned type metadata.
	case typeSpecNil:
		return TypeSpecNil{}
	default:
		panic(fmt.Errorf("unknown header: %d", header)) // Escapes: error formatting.
	}
}
