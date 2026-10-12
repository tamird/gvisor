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

import "gvisor.dev/gvisor/pkg/state/wire"

type signed interface {
	~int | ~int8 | ~int16 | ~int32 | ~int64
}
type unsigned interface {
	~uint | ~uint8 | ~uint16 | ~uint32 | ~uint64 | ~uintptr
}

// storeBool and the other scalar stores are shared by generated field helpers
// and the dynamic destination dispatcher. false reports a wire-kind mismatch;
// numeric truncation retains the existing write-before-error behavior.
func storeBool[T ~bool](dst *T, encoded wire.Value) bool {
	if encoded.Kind() != wire.BoolKind {
		return false
	}
	*dst = T(encoded.Bool())
	return true
}

func storeSigned[T signed](dst *T, encoded wire.Value) bool {
	if encoded.Kind() != wire.IntKind {
		return false
	}
	x := encoded.Int()
	v := T(x)
	*dst = v
	if got := int64(v); got != int64(x) {
		Failf("signed integer truncated from %v to %v", x, got)
	}
	return true
}

func storeUnsigned[T unsigned](dst *T, encoded wire.Value) bool {
	if encoded.Kind() != wire.UintKind {
		return false
	}
	x := encoded.Uint()
	v := T(x)
	*dst = v
	if got := uint64(v); got != uint64(x) {
		Failf("unsigned integer truncated from %v to %v", x, got)
	}
	return true
}

func storeFloat[T ~float32 | ~float64](dst *T, encoded wire.Value) bool {
	switch encoded.Kind() {
	case wire.Float32Kind:
		x := encoded.Float32()
		*dst = T(x)
	case wire.Float64Kind:
		x := encoded.Float64()
		v := T(x)
		*dst = v
		if got := float64(v); !isFloatEq(got, float64(x)) {
			Failf("floating point number truncated from %v to %v", x, got)
		}
	default:
		return false
	}
	return true
}

func storeComplex[T ~complex64 | ~complex128](dst *T, encoded wire.Value) bool {
	switch encoded.Kind() {
	case wire.Complex64Kind:
		x := encoded.Complex64()
		*dst = T(x)
	case wire.Complex128Kind:
		x := encoded.Complex128()
		v := T(x)
		*dst = v
		if got := complex128(v); !isComplexEq(got, complex128(x)) {
			Failf("complex number truncated from %v to %v", x, got)
		}
	default:
		return false
	}
	return true
}

func storeString[T ~string](dst *T, encoded wire.Value) bool {
	if encoded.Kind() != wire.StringKind {
		return false
	}
	*dst = T(encoded.Text())
	return true
}
