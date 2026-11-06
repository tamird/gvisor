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

// tcpdump_sender sends the two loopback datagrams checked by TestTcpdump.
package main

import (
	"log"
	"net"
	"net/netip"
)

func send() error {
	loopback := netip.AddrFrom4([4]byte{127, 0, 0, 1})
	address := net.UDPAddrFromAddrPort(netip.AddrPortFrom(loopback, 9999))
	conn, err := net.DialUDP("udp4", address, address)
	if err != nil {
		return err
	}
	defer conn.Close()
	for _, payload := range []string{"test", "testtest"} {
		if _, err := conn.Write([]byte(payload)); err != nil {
			return err
		}
	}
	return nil
}

func main() {
	if err := send(); err != nil {
		log.Fatal(err)
	}
}
