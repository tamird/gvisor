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
	"time"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/context"
	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/waiter"
)

// openPCIPipe substitutes the read end of a pipe for the fd returned by
// VFIO_GROUP_GET_DEVICE_FD. The PCI descriptor owns that fd just as it does
// the device; the caller can make it readable by writing to the other end.
func openPCIPipe(t *testing.T) (*pciDeviceFD, int) {
	t.Helper()
	var pipe [2]int
	if err := unix.Pipe2(pipe[:], unix.O_CLOEXEC|unix.O_NONBLOCK); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { unix.Close(pipe[1]) })
	group := tpuFD{device: &tpuDevice{tpuproxy: &tpuproxy{}}}
	fd, err := group.newPCIDeviceFD(int32(pipe[0]), "")
	if err != nil {
		unix.Close(pipe[0])
		t.Fatal(err)
	}
	t.Cleanup(func() { fd.Release(context.Background()) })
	return fd, pipe[1]
}

func TestPCIDeviceWake(t *testing.T) {
	fd, writer := openPCIPipe(t)
	entry, wake := waiter.NewChannelEntry(waiter.EventIn)
	if err := fd.EventRegister(&entry); err != nil {
		t.Fatal(err)
	}
	defer fd.EventUnregister(&entry)
	if _, err := unix.Write(writer, []byte{1}); err != nil {
		t.Fatal(err)
	}
	select {
	case <-wake:
	case <-time.After(5 * time.Second):
		t.Fatal("PCI device did not wake its waiter when its host fd became readable")
	}
}

func TestPCIDeviceRestoredWaiter(t *testing.T) {
	fd, _ := openPCIPipe(t)
	var entry waiter.Entry
	entry.Init(waiter.NoopListener{}, waiter.EventIn)
	if err := fd.EventRegister(&entry); err != nil {
		t.Fatal(err)
	}

	// Retain a second reference to the watch just as an epoll file does.
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
	if got, want := device.queue.Events(), waiter.EventIn; got != want {
		t.Fatalf("restored queue mask: got %v, want %v", got, want)
	}
	device.EventUnregister(watch)
	if !device.queue.IsEmpty() {
		t.Fatal("removing the restored watch left it on the PCI device")
	}
	if err := device.EventRegister(watch); err != nil {
		t.Fatal(err)
	}
	defer device.EventUnregister(watch)
	if got, want := device.queue.Events(), waiter.EventIn; got != want {
		t.Errorf("new watch mask: got %v, want %v", got, want)
	}
	if got, want := device.Readiness(waiter.AllEvents), waiter.EventMask(0); got != want {
		t.Errorf("restored PCI device readiness: got %v, want %v", got, want)
	}
}
