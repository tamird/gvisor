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

package vfio

import (
	"bytes"
	"testing"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/waiter"
)

func TestPCIDevicePoll(t *testing.T) {
	// Like vfio_device_fops, Linux's /dev/null has no poll operation.
	hostFD, err := unix.Open("/dev/null", unix.O_RDWR|unix.O_CLOEXEC, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer unix.Close(hostFD)
	ep, err := unix.EpollCreate1(unix.EPOLL_CLOEXEC)
	if err != nil {
		t.Fatal(err)
	}
	defer unix.Close(ep)
	if got, want := unix.EpollCtl(ep, unix.EPOLL_CTL_ADD, hostFD, &unix.EpollEvent{Events: unix.EPOLLIN}), unix.EPERM; got != want {
		t.Fatalf("epoll_ctl(/dev/null): got %v, want %v", got, want)
	}

	fd := pciDeviceFD{hostFD: int32(hostFD)}
	if fd.EpollMaskUpdater() != nil {
		t.Error("PCI device admits an epoll watch")
	}
	mask := waiter.AllEvents
	pfds := []unix.PollFd{{Fd: fd.hostFD, Events: int16(mask)}}
	if _, err := unix.Poll(pfds, 0); err != nil {
		t.Fatal(err)
	}
	if got, want := fd.Readiness(mask), waiter.EventMask(pfds[0].Revents); got != want {
		t.Errorf("live PCI readiness: got %v, want %v", got, want)
	}
}

func TestPCIDeviceRestoredWaiter(t *testing.T) {
	fd := &pciDeviceFD{hostFD: -1, tpuproxy: &tpuproxy{}}
	var entry waiter.Entry
	entry.Init(waiter.NoopListener{}, waiter.EventIn)
	// Older checkpoints can contain epoll watches on a PCI descriptor.
	// Save the reference normally held by epoll alongside the device.
	fd.queue.EventRegister(&entry)
	saved := [2]any{fd, &entry}
	var buf bytes.Buffer
	if _, err := state.Save(t.Context(), &buf, &saved); err != nil {
		t.Fatal(err)
	}
	var restored [2]any
	if _, err := state.Load(t.Context(), &buf, &restored); err != nil {
		t.Fatal(err)
	}
	device := restored[0].(*pciDeviceFD)
	watch := restored[1].(*waiter.Entry)
	device.EventUnregister(watch)
	if !device.queue.IsEmpty() {
		t.Fatal("removing the restored watch left it on the PCI device")
	}
	if err := device.EventRegister(watch); err != nil {
		t.Fatal(err)
	}
	defer device.EventUnregister(watch)
	if got, want := device.queue.Events(), waiter.EventIn; got != want {
		t.Errorf("new wait mask: got %v, want %v", got, want)
	}
	if got, want := device.Readiness(waiter.AllEvents), waiter.EventMask(0); got != want {
		t.Errorf("restored PCI device readiness: got %v, want %v", got, want)
	}
}
