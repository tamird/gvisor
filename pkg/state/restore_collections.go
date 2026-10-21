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
	if object.IsNil() {
		object.Set(reflect.MakeMap(object.Type()))
	}
	if len(encoded.Keys) == 0 {
		return
	}
	keyType, valueType := object.Type().Key(), object.Type().Elem()
	// Other kinds are copied into the map; interface values get independent
	// dynamic storage in decodeInterface.
	reuseKey := keyType.Kind() != reflect.Struct && keyType.Kind() != reflect.Array
	reuseValue := valueType.Kind() != reflect.Struct && valueType.Kind() != reflect.Array
	var keyCell, valueCell reflect.Value
	for i, key := range encoded.Keys {
		value := encoded.Values[i]
		if i == 0 || !reuseKey {
			keyCell = reflect.New(keyType).Elem()
		} else {
			// Nil wire values leave the destination unchanged.
			keyCell.SetZero()
		}
		if i == 0 || !reuseValue {
			valueCell = reflect.New(valueType).Elem()
		} else {
			valueCell.SetZero()
		}
		ds.restoreValue(ods, keyCell, key)
		ds.restoreValue(ods, valueCell, value)
		ds.waitObject(ods, key, nil)
		ds.waitObject(ods, value, nil)
		object.SetMapIndex(keyCell, valueCell)
	}
}
