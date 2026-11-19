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

package main

import (
	"net"
	"os"
	"os/exec"
	"reflect"
	"runtime"
	"syscall"
	"testing"

	"github.com/google/gopacket/pcapgo"
	"golang.org/x/sys/unix"
)

func TestWorkerRequirements(t *testing.T) {
	var uts unix.Utsname
	if err := unix.Uname(&uts); err != nil {
		t.Fatal(err)
	}
	t.Logf("worker kernel %s/%s", unix.ByteSliceToString(uts.Release[:]), unix.ByteSliceToString(uts.Machine[:]))
	if os.Getenv("PACKETIMPACT_WORKER_CHILD") != "1" {
		parentNS, err := os.Readlink("/proc/self/ns/net")
		if err != nil {
			t.Fatal(err)
		}
		interfaces, err := net.Interfaces()
		if err != nil {
			t.Fatal(err)
		}
		cmd := exec.CommandContext(t.Context(), "/proc/self/exe", "-test.run=^TestWorkerRequirements$", "-test.v")
		cmd.Env = append(os.Environ(), "PACKETIMPACT_WORKER_CHILD=1")
		cmd.SysProcAttr = &syscall.SysProcAttr{
			Cloneflags:  unix.CLONE_NEWUSER | unix.CLONE_NEWNET,
			Pdeathsig:   unix.SIGTERM,
			UidMappings: []syscall.SysProcIDMap{{ContainerID: 0, HostID: os.Getuid(), Size: 1}},
			GidMappings: []syscall.SysProcIDMap{{ContainerID: 0, HostID: os.Getgid(), Size: 1}},
		}
		output, runErr := cmd.CombinedOutput()
		t.Logf("packet worker linux/%s: %s", runtime.GOARCH, output)
		if runErr != nil {
			t.Errorf("packet worker setup: %v", runErr)
		}
		currentNS, err := os.Readlink("/proc/self/ns/net")
		if err != nil || currentNS != parentNS {
			t.Errorf("parent network namespace changed: got %q, want %q: %v", currentNS, parentNS, err)
		}
		currentInterfaces, err := net.Interfaces()
		if err != nil || !reflect.DeepEqual(currentInterfaces, interfaces) {
			t.Errorf("parent network interfaces changed: got %v, want %v: %v", currentInterfaces, interfaces, err)
		}
		return
	}

	// Create the real runner's links and nested DUT namespace without starting
	// a DUT or building its C++ RPC server.
	d, err := newDUT(t.Context(), 0, "/proc/self/exe", nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		for _, f := range append(d.cmd.ExtraFiles, d.completeR) {
			if err := f.Close(); err != nil {
				t.Error(err)
			}
		}
		if err := unix.Close(int(d.dutNetNS)); err != nil {
			t.Error(err)
		}
	})
	for _, iptables := range []string{"/sbin/iptables-nft", "/sbin/ip6tables-nft"} {
		cmd := exec.CommandContext(t.Context(), iptables, "-A", "INPUT", "-i", d.peerIface(), "--proto", "tcp", "-j", "DROP")
		if output, err := cmd.CombinedOutput(); err != nil {
			listing, listErr := exec.CommandContext(t.Context(), iptables, "-L", "-n", "-v").CombinedOutput()
			t.Logf("%s tables: %s (%v)", iptables, listing, listErr)
			t.Fatalf("%s: %v: %s", iptables, err, output)
		}
	}
	handle, err := pcapgo.NewEthernetHandle(d.peerIface())
	if err != nil {
		t.Fatal(err)
	}
	handle.Close()
}
