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
	"net/netip"
	"reflect"
)

// netipAddr supplies state methods without exposing the address's private
// representation. Restoring through netip's binary API also restores its
// canonical internal handles, so addresses remain usable as map keys.
type netipAddr netip.Addr

func init() {
	register((*netipAddr)(nil), reflect.TypeFor[*netip.Addr]())
}

// stateObject gives external values their state methods without changing the
// actual types held in fields, maps, or interfaces.
func stateObject(obj any) any {
	if addr, ok := obj.(*netip.Addr); ok {
		return (*netipAddr)(addr)
	}
	return obj
}

func (*netipAddr) StateTypeName() string { return "net/netip.Addr" }

func (*netipAddr) StateFields() []string { return []string{"value"} }

func (a *netipAddr) StateSave(s Sink) {
	data, err := netip.Addr(*a).MarshalBinary()
	if err != nil {
		Failf("encoding netip.Addr: %w", err)
	}
	// Strings decode inline. A deferred slice could leave a map key incomplete
	// when decodeMap inserts it into the restored map.
	s.SaveValue(0, string(data))
}

func (a *netipAddr) StateLoad(_ context.Context, s Source) {
	var data string
	s.Load(0, &data)
	if err := (*netip.Addr)(a).UnmarshalBinary([]byte(data)); err != nil {
		Failf("decoding netip.Addr: %w", err)
	}
}
