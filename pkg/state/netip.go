// Copyright 2026 The gVisor Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package state

import (
	"context"
	"encoding"
	"net/netip"
	"reflect"
)

// binaryState gives netip values state methods through their public binary
// encoding. Reconstructing their canonical internal handles preserves equality
// and map lookups without depending on the types' private representation.
type binaryState[T any, P interface {
	*T
	encoding.BinaryMarshaler
	encoding.BinaryUnmarshaler
}] struct {
	value P
}

func init() {
	register(binaryState[netip.Addr, *netip.Addr]{}, reflect.TypeFor[*netip.Addr]())
	register(binaryState[netip.Prefix, *netip.Prefix]{}, reflect.TypeFor[*netip.Prefix]())
	register(binaryState[netip.AddrPort, *netip.AddrPort]{}, reflect.TypeFor[*netip.AddrPort]())
}

// stateObject gives external values their state methods without changing the
// actual types held in fields, maps, or interfaces.
func stateObject(obj any) any {
	switch value := obj.(type) {
	case *netip.Addr:
		return binaryState[netip.Addr, *netip.Addr]{value}
	case *netip.Prefix:
		return binaryState[netip.Prefix, *netip.Prefix]{value}
	case *netip.AddrPort:
		return binaryState[netip.AddrPort, *netip.AddrPort]{value}
	default:
		return obj
	}
}

func (binaryState[T, P]) StateTypeName() string {
	typ := reflect.TypeFor[T]()
	return typ.PkgPath() + "." + typ.Name()
}

func (binaryState[T, P]) StateFields() []string { return []string{"value"} }

func (b binaryState[T, P]) StateSave(s Sink) {
	data, err := b.value.MarshalBinary()
	if err != nil {
		Failf("encoding %s: %w", b.StateTypeName(), err)
	}
	// Strings decode inline. A deferred slice could leave a map key incomplete
	// when decodeMap inserts it into the restored map.
	s.SaveValue(0, string(data))
}

func (b binaryState[T, P]) StateLoad(_ context.Context, s Source) {
	var data string
	s.Load(0, &data)
	if err := b.value.UnmarshalBinary([]byte(data)); err != nil {
		Failf("decoding %s: %w", b.StateTypeName(), err)
	}
}
