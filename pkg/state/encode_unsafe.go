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
	"reflect"
	"unsafe"
)

// arrayFromSlice constructs a new pointer to the slice data.
//
// The caller chooses the array length: capacity identifies shared storage;
// length identifies the elements that can be read from this slice.
func arrayFromSlice(obj reflect.Value, n int) reflect.Value {
	return reflect.NewAt(reflect.ArrayOf(n, obj.Type().Elem()), obj.UnsafePointer())
}

func copyBackingView(dst reflect.Value, src reflect.Value, offset uintptr) {
	ptr := unsafe.Add(dst.Addr().UnsafePointer(), offset)
	reflect.NewAt(src.Type().Elem(), ptr).Elem().Set(reflect.NewAt(src.Type().Elem(), src.UnsafePointer()).Elem())
}
