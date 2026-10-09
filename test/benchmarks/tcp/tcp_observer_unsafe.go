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

package main

import (
	"fmt"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/abi/linux"
)

// readNativeTCPInfo must run inside RawConn.Control. The x/sys TCP_INFO helper
// discards optlen; retain it so fields absent on older kernels stay unknown.
// Reuse the Linux ABI layout and marshaller instead of defining another one.
func readNativeTCPInfo(fd uintptr) (tcpNativeObservation, error) {
	var info linux.TCPInfo
	buf := make([]byte, info.SizeBytes())
	length := uint32(len(buf))
	_, _, errno := unix.Syscall6(unix.SYS_GETSOCKOPT, fd, unix.IPPROTO_TCP, unix.TCP_INFO, uintptr(unsafe.Pointer(&buf[0])), uintptr(unsafe.Pointer(&length)), 0)
	if errno != 0 {
		return tcpNativeObservation{}, errno
	}
	baseEnd := unsafe.Offsetof(info.TotalRetrans) + unsafe.Sizeof(info.TotalRetrans)
	if uintptr(length) < baseEnd || uint64(length) > uint64(len(buf)) {
		return tcpNativeObservation{}, fmt.Errorf("TCP_INFO returned %d bytes, want %d..%d", length, baseEnd, len(buf))
	}
	// buf was zeroed, but only fields whose entire extent was returned are
	// exposed below. UnmarshalBytes needs the complete, zero-padded buffer.
	info.UnmarshalBytes(buf)
	cc, err := unix.GetsockoptString(int(fd), unix.IPPROTO_TCP, unix.TCP_CONGESTION)
	if err != nil {
		return tcpNativeObservation{}, err
	}
	result := tcpNativeObservation{
		InfoBytes:         length,
		CongestionControl: cc,
		State:             info.State,
		CongestionState:   info.CaState,
		Cwnd:              info.SndCwnd,
		Ssthresh:          info.SndSsthresh,
		MSS:               info.SndMss,
		RTT:               time.Duration(info.RTT) * time.Microsecond,
		RTTVar:            time.Duration(info.RTTVar) * time.Microsecond,
		RTO:               time.Duration(info.RTO) * time.Microsecond,
		Unacked:           info.Unacked,
		Sacked:            info.Sacked,
		Lost:              info.Lost,
		Retrans:           info.Retrans,
		TotalRetrans:      info.TotalRetrans,
	}
	if uintptr(length) >= unsafe.Offsetof(info.BytesAcked)+unsafe.Sizeof(info.BytesAcked) {
		result.BytesAcked = &info.BytesAcked
	}
	if uintptr(length) >= unsafe.Offsetof(info.NotSentBytes)+unsafe.Sizeof(info.NotSentBytes) {
		result.NotSentBytes = &info.NotSentBytes
	}
	if uintptr(length) >= unsafe.Offsetof(info.DeliveryRate)+unsafe.Sizeof(info.DeliveryRate) {
		result.DeliveryRate = &info.DeliveryRate
	}
	if uintptr(length) >= unsafe.Offsetof(info.BusyTime)+unsafe.Sizeof(info.BusyTime) {
		value := time.Duration(info.BusyTime) * time.Microsecond
		result.BusyTime = &value
	}
	if uintptr(length) >= unsafe.Offsetof(info.RwndLimited)+unsafe.Sizeof(info.RwndLimited) {
		value := time.Duration(info.RwndLimited) * time.Microsecond
		result.ReceiveWindowLimited = &value
	}
	if uintptr(length) >= unsafe.Offsetof(info.SndBufLimited)+unsafe.Sizeof(info.SndBufLimited) {
		value := time.Duration(info.SndBufLimited) * time.Microsecond
		result.SendBufferLimited = &value
	}
	return result, nil
}
