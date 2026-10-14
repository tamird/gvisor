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
	"math/bits"

	"gvisor.dev/gvisor/pkg/gohacks"
)

// frameKind is an internal Value representation, never an accepted wire tag.
const frameKind = Kind(typeType + 1)

// frameNode contains decoded numbers and indices, never disguised Go pointers.
// Children are addressed by index so growing nodes cannot invalidate a view.
type frameNode struct {
	kind Kind
	a, b uint64
}

type valueFrame struct {
	valueHeader
	nodes   frameNodes
	strings []string
}

// ValueBuffer reuses temporary storage for completely parsed values. A value
// returned by Load is valid only until the next Load or Release. Own detaches a
// subtree using Value's ordinary owned representation. Strings always have
// independent immutable backing storage, including in temporary values.
// A ValueBuffer must not be copied after first use or used concurrently.
type ValueBuffer struct {
	frame valueFrame
}

// Load parses one complete value before returning it. It does not restore any
// destination or run callbacks while parsing.
//
// +checkescape:hard
func (b *ValueBuffer) Load(r *Reader) Value {
	clear(b.frame.strings)
	b.frame.strings = b.frame.strings[:0]
	b.frame.nodes.length = 0
	b.frame.kind = frameKind
	index := b.frame.reserve(1)
	b.frame.loadValue(r, index)
	return frameValue(&b.frame, index)
}

// Release drops all backing storage. In particular, retaining a Source must
// not retain a Load's largest unrelated temporary frame through decodeState.
func (b *ValueBuffer) Release() {
	b.frame.nodes = frameNodes{}
	b.frame.strings = nil
}

// frameNodes grows without copying existing nodes. Block sizes are 4, 8,
// 16, ...; the small first block avoids reserving a large page for tiny values.
// Only the block headers move when their slice grows. Logical indices remain
// stable, including when a reserved child range crosses block boundaries.
type frameNodes struct {
	blocks   [][]frameNode
	length   int
	capacity int
}

func (n *frameNodes) at(index int) *frameNode {
	if uint(index) >= uint(n.length) {
		panic("decoded node index out of bounds")
	}
	// Add in the unsigned domain: a valid index near MaxInt must not wrap.
	x := uint(index) + 4
	shift := bits.Len(x) - 1
	return &n.blocks[shift-2][int(x-(uint(1)<<shift))]
}

func (f *valueFrame) node(index int) frameNode {
	return *f.nodes.at(index)
}

func (f *valueFrame) setNode(index int, node frameNode) {
	*f.nodes.at(index) = node
}

// reserve returns uninitialized slots. Every parser path assigns a complete
// frameNode before reading a slot or returning the parsed value, so reused
// pointer-free storage needs no clearing.
func (f *valueFrame) reserve(count uint64) int {
	n := &f.nodes
	const maxInt = int(^uint(0) >> 1)
	if count > uint64(maxInt-n.length) {
		panic("decoded value is too large")
	}
	first := n.length
	end := first + int(count)
	for n.capacity < end {
		// The final block may be shorter so capacity stays representable.
		size := min(uint(n.capacity)+4, uint(maxInt-n.capacity))
		block := make([]frameNode, int(size)) // Escapes: temporary decoded nodes.
		n.blocks = append(n.blocks, block)    // Escapes: temporary block index.
		n.capacity += int(size)
	}
	n.length = end
	return first
}

func (f *valueFrame) text(value string) uint64 {
	index := len(f.strings)
	f.strings = append(f.strings, value) // Escapes: immutable decoded string headers.
	return uint64(index)
}

func (f *valueFrame) loadValue(r *Reader, index int) {
	f.loadPayload(r, index, Kind(loadUint(r)))
}

// loadPayload is the sole data-value grammar. Array and map elements retain
// the existing omission of repeated tags after their first element.
func (f *valueFrame) loadPayload(r *Reader, index int, kind Kind) {
	node := frameNode{kind: kind}
	switch kind {
	case KindBool:
		if loadBool(r) {
			node.a = 1
		}
	case KindInt:
		node.a = uint64(loadInt(r))
	case KindUint, KindFloat64:
		node.a = uint64(loadUint(r))
	case KindFloat32:
		node.a = uint64(uint32(loadUint(r)))
	case KindComplex64:
		re, im := uint32(loadUint(r)), uint32(loadUint(r))
		node.a = uint64(re) | uint64(im)<<32
	case KindComplex128:
		node.a, node.b = uint64(loadUint(r)), uint64(loadUint(r))
	case KindNil, KindNoObjects:
	case KindRef:
		f.loadReference(r, index)
		return
	case KindString:
		value := string(loadString(r))
		node.a = f.text(value)
	case KindSlice:
		length, capacity := loadUint(r), loadUint(r)
		bounds := f.reserve(1)
		f.setNode(bounds, frameNode{a: uint64(length), b: uint64(capacity)})
		ref := f.reserve(1)
		f.loadReference(r, ref)
		node.a, node.b = uint64(bounds), uint64(ref)
	case KindArray:
		count := uint64(loadUint(r))
		first := f.reserve(count)
		node.a, node.b = uint64(first), count
		if count != 0 {
			f.loadValue(r, first)
			childKind := f.node(first).kind
			for i := 1; i < int(count); i++ {
				f.loadPayload(r, first+i, childKind)
			}
		}
	case KindMap:
		count := uint64(loadUint(r))
		if count > uint64(int(^uint(0)>>1))/2 {
			panic("decoded map is too large")
		}
		first := f.reserve(2 * count)
		node.a, node.b = uint64(first), count
		if count != 0 {
			f.loadValue(r, first)
			f.loadValue(r, first+1)
			keyKind, valueKind := f.node(first).kind, f.node(first+1).kind
			for i := 1; i < int(count); i++ {
				f.loadPayload(r, first+2*i, keyKind)
				f.loadPayload(r, first+2*i+1, valueKind)
			}
		}
	case KindStruct:
		node.a = uint64(loadUint(r))
		fields := f.reserve(1)
		f.loadValue(r, fields)
		node.b = uint64(fields)
	case KindMultipleObjects:
		count := uint64(loadUint(r))
		first := f.reserve(count)
		node.a, node.b = uint64(first), count
		for i := 0; i < int(count); i++ {
			f.loadValue(r, first+i)
		}
	case KindInterface:
		node.a = uint64(f.loadType(r))
		value := f.reserve(1)
		f.loadValue(r, value)
		node.b = uint64(value)
	case KindType:
		name := string(loadString(r))
		node.a = f.text(name)
		count := uint64(loadUint(r))
		fields := f.reserve(1)
		first := f.reserve(count)
		f.setNode(fields, frameNode{a: uint64(first), b: count})
		node.b = uint64(fields)
		for i := 0; i < int(count); i++ {
			name := string(loadString(r))
			f.setNode(first+i, frameNode{a: f.text(name)})
		}
	default:
		panic(fmt.Errorf("unknown header: %d", kind)) // Escapes: error formatting.
	}
	// Publish the completed node after its recursive payload has been parsed.
	f.setNode(index, node)
}

func (f *valueFrame) loadReference(r *Reader, index int) {
	node := frameNode{kind: KindRef, a: uint64(loadUint(r))}
	count := uint64(loadUint(r))
	if count != 0 {
		metadata := f.reserve(1)
		first := f.reserve(count)
		for i := 0; i < int(count); i++ {
			dot := loadInt(r)
			if dot >= 0 {
				// Preserve Index's existing narrowing, independently of the
				// full-width reference root and type metadata.
				f.setNode(first+i, frameNode{a: uint64(Index(dot))})
				continue
			}
			data := make([]byte, -dot) // Escapes: independently owned field name.
			readFull(r, data)
			name := gohacks.StringFromImmutableBytes(data)
			f.setNode(first+i, frameNode{kind: 1, a: f.text(name)})
		}
		typ := f.loadType(r)
		f.setNode(metadata, frameNode{kind: Kind(count), a: uint64(first), b: uint64(typ)})
		node.b = uint64(metadata) + 1 // Zero denotes the pathless reference.
	}
	f.setNode(index, node)
}

// loadType is the sole TypeSpec grammar. The tag belongs to the separate
// TypeSpec namespace, not to the data-value namespace.
func (f *valueFrame) loadType(r *Reader) int {
	index := f.reserve(1)
	header := loadUint(r)
	node := frameNode{kind: Kind(header)}
	switch header {
	case typeSpecTypeID:
		node.a = uint64(loadUint(r))
	case typeSpecPointer, typeSpecSlice:
		node.a = uint64(f.loadType(r))
	case typeSpecArray:
		node.a = uint64(loadUint(r))
		node.b = uint64(f.loadType(r))
	case typeSpecMap:
		node.a = uint64(f.loadType(r))
		node.b = uint64(f.loadType(r))
	case typeSpecNil:
	default:
		panic(fmt.Errorf("unknown header: %d", header)) // Escapes: error formatting.
	}
	f.setNode(index, node)
	return index
}
