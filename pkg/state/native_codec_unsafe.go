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
	"context"
	"unsafe"

	"gvisor.dev/gvisor/pkg/state/wire"
)

// nativeCodec contains static generated operations for one exact struct type.
// It is copied into registry rows and immutable operation graphs, never boxed
// into a separate descriptor or captured by a per-type factory closure.
type nativeCodec struct {
	newValue  func() unsafe.Pointer
	loadValue func(context.Context, unsafe.Pointer, Source)
}

// RegisterGenerated registers generated capture and load operations in the
// same type database as Register, during init and once per type. newRecord
// may be nil for a manual saver.
// The native callbacks must both be nil or both describe the exact concrete
// type pointed to by t: newValue allocates that Go type, and loadValue invokes
// its StateLoad method at the supplied writable address. They must not retain
// Source or depend on uninitialized generated globals.
func RegisterGenerated(t Type, newRecord func(wire.TypeID, int) *wire.Record, newValue func() unsafe.Pointer, loadValue func(context.Context, unsafe.Pointer, Source)) {
	if (newValue == nil) != (loadValue == nil) {
		Failf("incomplete native codec for %T", t)
	}
	if newRecord == nil && newValue == nil {
		Failf("missing generated operations for %T", t)
	}
	registerState(t, newRecord, nativeCodec{newValue: newValue, loadValue: loadValue})
}
