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

// BeginSnapshot allocates one record for exactly the type's saved fields.
// Call it once from StateSave, before capturing fields. emit must write those
// fields in wire order without consulting the original object or rerunning
// save hooks. Ordinary Sink.Save and Sink.SaveValue must not be mixed with a
// typed record; Capture and CaptureValue populate its stable child slots.
func BeginSnapshot[T any](s Sink, emit func(*wire.Writer, *T)) *T {
	return wire.AllocSnapshot(s.internal.encoded, len(s.internal.typ.Fields), emit)
}

// Capture saves an addressable child into a stable slot owned by the record.
// Graph discovery and late reference reparenting use the existing resolver.
func (s Sink) Capture(objPtr any, dest *wire.Object) {
	s.internal.es.encodeObject(reflect.ValueOf(objPtr).Elem(), encodeDefault, dest)
}

// CaptureValue saves a custom value at the same boundary as Sink.SaveValue.
func (s Sink) CaptureValue(value any, dest *wire.Object) {
	s.internal.es.encodeObject(reflect.ValueOf(value), encodeDefault, dest)
}
