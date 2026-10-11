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

package stack

import (
	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/state/wire"
	"gvisor.dev/gvisor/pkg/tcpip"
)

// Explicit fixed records model generated output for the measured network graph.
// Capture preserves stateify order and excludes ignored fields and locks.
type headerInfoSnapshot struct {
	offset int
	length int
}

func emitHeaderInfoSnapshot(w *wire.Writer, snapshot *headerInfoSnapshot) {
	wire.SaveIntField(w, int64(snapshot.offset))
	wire.SaveIntField(w, int64(snapshot.length))
}

func (value *headerInfo) StateSave(s state.Sink) {
	value.beforeSave()
	snapshot := state.BeginSnapshot(s, emitHeaderInfoSnapshot)
	snapshot.offset = value.offset
	snapshot.length = value.length
}

type networkPacketInfoSnapshot struct {
	LocalAddressBroadcast bool
	LocalAddressTemporary bool
	IsForwardedPacket     bool
}

func emitNetworkPacketInfoSnapshot(w *wire.Writer, snapshot *networkPacketInfoSnapshot) {
	wire.SaveBoolField(w, snapshot.LocalAddressBroadcast)
	wire.SaveBoolField(w, snapshot.LocalAddressTemporary)
	wire.SaveBoolField(w, snapshot.IsForwardedPacket)
}

func (value *NetworkPacketInfo) StateSave(s state.Sink) {
	value.beforeSave()
	snapshot := state.BeginSnapshot(s, emitNetworkPacketInfoSnapshot)
	snapshot.LocalAddressBroadcast = value.LocalAddressBroadcast
	snapshot.LocalAddressTemporary = value.LocalAddressTemporary
	snapshot.IsForwardedPacket = value.IsForwardedPacket
}

type gsoSnapshot struct {
	Type       GSOType
	NeedsCsum  bool
	CsumOffset uint16
	MSS        uint16
	L3HdrLen   uint16
	MaxSize    uint32
}

func emitGsoSnapshot(w *wire.Writer, snapshot *gsoSnapshot) {
	wire.SaveIntField(w, int64(snapshot.Type))
	wire.SaveBoolField(w, snapshot.NeedsCsum)
	wire.SaveUintField(w, uint64(snapshot.CsumOffset))
	wire.SaveUintField(w, uint64(snapshot.MSS))
	wire.SaveUintField(w, uint64(snapshot.L3HdrLen))
	wire.SaveUintField(w, uint64(snapshot.MaxSize))
}

func (value *GSO) StateSave(s state.Sink) {
	value.beforeSave()
	snapshot := state.BeginSnapshot(s, emitGsoSnapshot)
	snapshot.Type = value.Type
	snapshot.NeedsCsum = value.NeedsCsum
	snapshot.CsumOffset = value.CsumOffset
	snapshot.MSS = value.MSS
	snapshot.L3HdrLen = value.L3HdrLen
	snapshot.MaxSize = value.MaxSize
}

type routeInfoSnapshot struct {
	RemoteAddress    wire.Object
	LocalAddress     wire.Object
	LocalLinkAddress tcpip.LinkAddress
	NextHop          wire.Object
	NetProto         tcpip.NetworkProtocolNumber
	Loop             PacketLooping
}

func emitRouteInfoSnapshot(w *wire.Writer, snapshot *routeInfoSnapshot) {
	wire.Save(w, snapshot.RemoteAddress)
	wire.Save(w, snapshot.LocalAddress)
	wire.SaveStringField(w, string(snapshot.LocalLinkAddress))
	wire.Save(w, snapshot.NextHop)
	wire.SaveUintField(w, uint64(snapshot.NetProto))
	wire.SaveUintField(w, uint64(snapshot.Loop))
}

func (value *routeInfo) StateSave(s state.Sink) {
	value.beforeSave()
	snapshot := state.BeginSnapshot(s, emitRouteInfoSnapshot)
	s.Capture(&value.RemoteAddress, &snapshot.RemoteAddress)
	s.Capture(&value.LocalAddress, &snapshot.LocalAddress)
	snapshot.LocalLinkAddress = value.LocalLinkAddress
	s.Capture(&value.NextHop, &snapshot.NextHop)
	snapshot.NetProto = value.NetProto
	snapshot.Loop = value.Loop
}

type exportedRouteInfoSnapshot struct {
	routeInfo         wire.Object
	RemoteLinkAddress tcpip.LinkAddress
}

func emitExportedRouteInfoSnapshot(w *wire.Writer, snapshot *exportedRouteInfoSnapshot) {
	wire.Save(w, snapshot.routeInfo)
	wire.SaveStringField(w, string(snapshot.RemoteLinkAddress))
}

func (value *RouteInfo) StateSave(s state.Sink) {
	value.beforeSave()
	snapshot := state.BeginSnapshot(s, emitExportedRouteInfoSnapshot)
	s.Capture(&value.routeInfo, &snapshot.routeInfo)
	snapshot.RemoteLinkAddress = value.RemoteLinkAddress
}

type packetBufferSnapshot struct {
	packetBufferRefs        wire.Object
	buf                     wire.Object
	reserved                int
	pushed                  int
	consumed                int
	headers                 wire.Object
	NetworkProtocolNumber   tcpip.NetworkProtocolNumber
	TransportProtocolNumber tcpip.TransportProtocolNumber
	Hash                    uint32
	Owner                   wire.Object
	EgressRoute             wire.Object
	GSOOptions              wire.Object
	snatDone                bool
	dnatDone                bool
	PktType                 tcpip.PacketType
	NICID                   tcpip.NICID
	InputNICID              tcpip.NICID
	RXChecksumValidated     bool
	NetworkPacketInfo       wire.Object
	Mark                    uint32
	tuple                   wire.Object
}

func emitPacketBufferSnapshot(w *wire.Writer, snapshot *packetBufferSnapshot) {
	wire.Save(w, snapshot.packetBufferRefs)
	wire.Save(w, snapshot.buf)
	wire.SaveIntField(w, int64(snapshot.reserved))
	wire.SaveIntField(w, int64(snapshot.pushed))
	wire.SaveIntField(w, int64(snapshot.consumed))
	wire.Save(w, snapshot.headers)
	wire.SaveUintField(w, uint64(snapshot.NetworkProtocolNumber))
	wire.SaveUintField(w, uint64(snapshot.TransportProtocolNumber))
	wire.SaveUintField(w, uint64(snapshot.Hash))
	wire.Save(w, snapshot.Owner)
	wire.Save(w, snapshot.EgressRoute)
	wire.Save(w, snapshot.GSOOptions)
	wire.SaveBoolField(w, snapshot.snatDone)
	wire.SaveBoolField(w, snapshot.dnatDone)
	wire.SaveUintField(w, uint64(snapshot.PktType))
	wire.SaveIntField(w, int64(snapshot.NICID))
	wire.SaveIntField(w, int64(snapshot.InputNICID))
	wire.SaveBoolField(w, snapshot.RXChecksumValidated)
	wire.Save(w, snapshot.NetworkPacketInfo)
	wire.SaveUintField(w, uint64(snapshot.Mark))
	wire.Save(w, snapshot.tuple)
}

func (pk *PacketBuffer) StateSave(s state.Sink) {
	pk.beforeSave()
	snapshot := state.BeginSnapshot(s, emitPacketBufferSnapshot)
	s.Capture(&pk.packetBufferRefs, &snapshot.packetBufferRefs)
	s.Capture(&pk.buf, &snapshot.buf)
	snapshot.reserved = pk.reserved
	snapshot.pushed = pk.pushed
	snapshot.consumed = pk.consumed
	s.Capture(&pk.headers, &snapshot.headers)
	snapshot.NetworkProtocolNumber = pk.NetworkProtocolNumber
	snapshot.TransportProtocolNumber = pk.TransportProtocolNumber
	snapshot.Hash = pk.Hash
	s.Capture(&pk.Owner, &snapshot.Owner)
	s.Capture(&pk.EgressRoute, &snapshot.EgressRoute)
	s.Capture(&pk.GSOOptions, &snapshot.GSOOptions)
	snapshot.snatDone = pk.snatDone
	snapshot.dnatDone = pk.dnatDone
	snapshot.PktType = pk.PktType
	snapshot.NICID = pk.NICID
	snapshot.InputNICID = pk.InputNICID
	snapshot.RXChecksumValidated = pk.RXChecksumValidated
	s.Capture(&pk.NetworkPacketInfo, &snapshot.NetworkPacketInfo)
	snapshot.Mark = pk.Mark
	s.Capture(&pk.tuple, &snapshot.tuple)
}
