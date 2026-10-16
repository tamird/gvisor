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

// restoreArray owns length checks, traversal and element waits. Index reads
// the live destination at each step, including when an earlier loader replaces
// a slice destination's header.
func (ds *decodeState) restoreArray(ods *objectDecodeState, target reflect.Value, encoded []wire.Object) {
	length := target.Len()
	if len(encoded) != length {
		Failf("mismatching array length expect=%d, actual=%d", length, len(encoded))
	}
	for i, value := range encoded {
		ds.restoreValue(ods, target.Index(i), value)
		ds.waitObject(ods, value, nil)
	}
}

// restoreMap keeps fresh typed cells and Go's map insertion semantics at the
// shared boundary. Both cells exist before hooks; waits precede insertion,
// which reads the live map header. Nonzero-sized cells are never reused.
func (ds *decodeState) restoreMap(ods *objectDecodeState, object reflect.Value, encoded *wire.Map) {
	if object.IsNil() {
		object.Set(reflect.MakeMap(object.Type()))
	}
	for i, key := range encoded.Keys {
		value := encoded.Values[i]
		keyCell := reflect.New(object.Type().Key()).Elem()
		valueCell := reflect.New(object.Type().Elem()).Elem()
		ds.restoreValue(ods, keyCell, key)
		ds.restoreValue(ods, valueCell, value)
		ds.waitObject(ods, key, nil)
		ds.waitObject(ods, value, nil)
		object.SetMapIndex(keyCell, valueCell)
	}
}
