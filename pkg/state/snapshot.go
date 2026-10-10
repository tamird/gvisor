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

// SnapshotSaver supplies a fixed typed snapshot instead of per-field objects.
// It must preserve StateSave's hook order and capture each saved value at the
// same point. The snapshot owns its values and all retained child handles.
type SnapshotSaver interface {
	StateSaveSnapshot(SnapshotSink)
}

// SnapshotSink captures children through the ordinary graph resolver. Primitive
// values are copied directly into the typed record returned by BeginSnapshot.
type SnapshotSink struct {
	internal objectEncoder
	fields   int
}

// BeginSnapshot allocates one record for exactly the type's saved fields.
// Call it once, before capturing children. emit must write those fields in wire
// order without consulting the original object or rerunning save hooks.
func BeginSnapshot[T any](s SnapshotSink, emit func(*wire.Writer, *T)) *T {
	return wire.AllocSnapshot(s.internal.encoded, s.fields, emit)
}

// Save captures an addressable child into a stable slot owned by the snapshot.
// Graph discovery and late reference reparenting use the existing resolver.
func (s SnapshotSink) Save(objPtr any, dest *wire.Object) {
	s.internal.es.encodeObject(reflect.ValueOf(objPtr).Elem(), encodeDefault, dest)
}

// SaveValue captures a custom value at the same boundary as Sink.SaveValue.
func (s SnapshotSink) SaveValue(value any, dest *wire.Object) {
	s.internal.es.encodeObject(reflect.ValueOf(value), encodeDefault, dest)
}
