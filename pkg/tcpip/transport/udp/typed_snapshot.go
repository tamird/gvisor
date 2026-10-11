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

package udp

import (
	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/state/wire"
	"gvisor.dev/gvisor/pkg/tcpip"
)

// Explicit fixed records model generated output for the measured network graph.
// Capture preserves stateify order and excludes ignored fields and locks.
type udpPacketSnapshot struct {
	udpPacketEntry     wire.Object
	netProto           tcpip.NetworkProtocolNumber
	senderAddress      wire.Object
	destinationAddress wire.Object
	packetInfo         wire.Object
	pkt                wire.Object
	receivedAt         int64
	tosOrTClass        uint8
	ttlOrHopLimit      uint8
}

func emitUDPPacketSnapshot(w *wire.Writer, snapshot *udpPacketSnapshot) {
	wire.Save(w, snapshot.udpPacketEntry)
	wire.SaveUintField(w, uint64(snapshot.netProto))
	wire.Save(w, snapshot.senderAddress)
	wire.Save(w, snapshot.destinationAddress)
	wire.Save(w, snapshot.packetInfo)
	wire.Save(w, snapshot.pkt)
	wire.SaveIntField(w, int64(snapshot.receivedAt))
	wire.SaveUintField(w, uint64(snapshot.tosOrTClass))
	wire.SaveUintField(w, uint64(snapshot.ttlOrHopLimit))
}

func (value *udpPacket) StateSave(s state.Sink) {
	value.beforeSave()
	snapshot := state.BeginSnapshot(s, emitUDPPacketSnapshot)
	// Custom values are captured before ordinary fields, as in stateify.
	snapshot.receivedAt = value.saveReceivedAt()
	s.Capture(&value.udpPacketEntry, &snapshot.udpPacketEntry)
	snapshot.netProto = value.netProto
	s.Capture(&value.senderAddress, &snapshot.senderAddress)
	s.Capture(&value.destinationAddress, &snapshot.destinationAddress)
	s.Capture(&value.packetInfo, &snapshot.packetInfo)
	s.Capture(&value.pkt, &snapshot.pkt)
	snapshot.tosOrTClass = value.tosOrTClass
	snapshot.ttlOrHopLimit = value.ttlOrHopLimit
}
