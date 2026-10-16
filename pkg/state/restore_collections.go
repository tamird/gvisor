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

// restoreArray owns length checks, traversal and element waits. Actual arrays
// use direct storage; legacy slice destinations reread their live header at
// each Index, including when a previous element's loader replaced that header.
func (ds *decodeState) restoreArray(ods *objectDecodeState, target restoreDestination, encoded []wire.Value) {
	length := target.length()
	if len(encoded) != length {
		Failf("mismatching array length expect=%d, actual=%d", length, len(encoded))
	}
	var element reflect.Type
	if length != 0 && target.directArray() {
		element = target.typ.Elem()
	}
	for i, value := range encoded {
		var child restoreDestination
		if element != nil {
			child = target.field(element, uintptr(i)*element.Size())
		} else {
			child = ds.destination(target.value().Index(i))
		}
		ds.restoreValue(ods, child, value)
		ds.waitObject(ods, value, nil)
	}
}

// restoreMap keeps fresh typed cells and Go's map insertion semantics at the
// shared boundary. Both cells exist before hooks; waits precede insertion,
// which reads the live map header. Nonzero-sized cells are never reused.
func (ds *decodeState) restoreMap(ods *objectDecodeState, target restoreDestination, encoded []wire.MapEntry) {
	object := target.value()
	if object.IsNil() {
		object.Set(reflect.MakeMap(object.Type()))
	}
	for _, entry := range encoded {
		keyCell := reflect.New(object.Type().Key()).Elem()
		valueCell := reflect.New(object.Type().Elem()).Elem()
		ds.restoreValue(ods, ds.destination(keyCell), entry.Key)
		ds.restoreValue(ods, ds.destination(valueCell), entry.Value)
		ds.waitObject(ods, entry.Key, nil)
		ds.waitObject(ods, entry.Value, nil)
		object.SetMapIndex(keyCell, valueCell)
	}
}
