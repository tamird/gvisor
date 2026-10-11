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

package tcpip

import (
	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/state/wire"
)

// Explicit fixed records model generated output for the measured network graph.
// Capture preserves stateify order and excludes ignored fields and locks.
type addressSnapshot struct {
	addr   wire.Object
	length int
}

func emitAddressSnapshot(w *wire.Writer, snapshot *addressSnapshot) {
	wire.Save(w, snapshot.addr)
	wire.SaveIntField(w, int64(snapshot.length))
}

func (a *Address) StateSave(s state.Sink) {
	a.beforeSave()
	snapshot := state.BeginSnapshot(s, emitAddressSnapshot)
	s.Capture(&a.addr, &snapshot.addr)
	snapshot.length = a.length
}

type fullAddressSnapshot struct {
	NIC      NICID
	Addr     wire.Object
	Port     uint16
	LinkAddr LinkAddress
}

func emitFullAddressSnapshot(w *wire.Writer, snapshot *fullAddressSnapshot) {
	wire.SaveIntField(w, int64(snapshot.NIC))
	wire.Save(w, snapshot.Addr)
	wire.SaveUintField(w, uint64(snapshot.Port))
	wire.SaveStringField(w, string(snapshot.LinkAddr))
}

func (value *FullAddress) StateSave(s state.Sink) {
	value.beforeSave()
	snapshot := state.BeginSnapshot(s, emitFullAddressSnapshot)
	snapshot.NIC = value.NIC
	s.Capture(&value.Addr, &snapshot.Addr)
	snapshot.Port = value.Port
	snapshot.LinkAddr = value.LinkAddr
}

type ipPacketInfoSnapshot struct {
	NIC             NICID
	LocalAddr       wire.Object
	DestinationAddr wire.Object
}

func emitIPPacketInfoSnapshot(w *wire.Writer, snapshot *ipPacketInfoSnapshot) {
	wire.SaveIntField(w, int64(snapshot.NIC))
	wire.Save(w, snapshot.LocalAddr)
	wire.Save(w, snapshot.DestinationAddr)
}

func (value *IPPacketInfo) StateSave(s state.Sink) {
	value.beforeSave()
	snapshot := state.BeginSnapshot(s, emitIPPacketInfoSnapshot)
	snapshot.NIC = value.NIC
	s.Capture(&value.LocalAddr, &snapshot.LocalAddr)
	s.Capture(&value.DestinationAddr, &snapshot.DestinationAddr)
}
