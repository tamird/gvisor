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
	"context"
	"fmt"
	"os"
	"os/exec"
	"runtime"
	"strconv"

	"golang.org/x/sys/unix"
)

// newNetNS keeps the namespace alive through fd. When the locked goroutine
// exits, Go discards its thread. Other threads remain in the original netns.
func newNetNS() (*os.File, error) {
	var fd *os.File
	var err error
	done := make(chan struct{})
	go func() {
		defer close(done)
		runtime.LockOSThread()
		if err = unix.Unshare(unix.CLONE_NEWNET); err != nil {
			return
		}
		fd, err = os.Open("/proc/thread-self/ns/net")
	}()
	<-done
	return fd, err
}

func (o *options) command(ctx context.Context, ns, program string, args ...string) *exec.Cmd {
	if ns != "" {
		args = append([]string{ns, program}, args...)
		program = o.nsjoin
	}
	if o.verbose {
		fmt.Fprintf(os.Stderr, "exec %q %q\n", program, args)
	}
	cmd := exec.CommandContext(ctx, program, args...)
	cmd.Stdout, cmd.Stderr = os.Stderr, os.Stderr
	return cmd
}

func (o *options) execute(ctx context.Context, ns, program string, args ...string) error {
	cmd := o.command(ctx, ns, program, args...)
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("%s: %v", cmd, err)
	}
	return nil
}

func (o *options) setup(ctx context.Context, clientNS, serverNS string) error {
	ip := func(args ...string) error { return o.execute(ctx, "", "ip", args...) }
	for _, side := range []string{"client", "server", "wan"} {
		if err := ip("link", "add", side+".0", "type", "veth", "peer", "name", side+".1"); err != nil {
			return err
		}
	}
	for i, side := range []string{"client", "server"} {
		bridge := "br" + strconv.Itoa(i)
		wan := "wan." + strconv.Itoa(i)
		for _, a := range [][]string{
			{"link", "add", "name", bridge, "type", "bridge"},
			{"link", "set", side + ".1", "master", bridge},
			{"link", "set", wan, "master", bridge},
			{"link", "set", bridge, "up"},
			{"link", "set", wan, "mtu", strconv.Itoa(o.mtu), "up"},
			{"link", "set", side + ".0", "mtu", strconv.Itoa(o.mtu)},
		} {
			if err := ip(a...); err != nil {
				return err
			}
		}
		// Both directions incur half the configured delay, loss and duplication.
		// Paretonormal jitter preserves the minimum RTT and adds small delay
		// spikes and reordering. The plain Pareto tail produces excessive delays.
		// Use independent loss: netem's correlation option is deprecated because
		// it produces unexpected loss patterns.
		tc := []string{"qdisc", "add", "dev", wan, "root", "netem", "delay", fmt.Sprintf("%.2fms", o.latency/2), fmt.Sprintf("%gms", o.jitter)}
		if fmt.Sprintf("%.2f", o.jitter) != "0.00" {
			tc = append(tc, "distribution", "paretonormal")
		}
		if o.loss != 0 {
			tc = append(tc, "loss", "random", fmt.Sprintf("%.6f%%", o.loss/2))
		}
		if o.duplicate != 0 {
			tc = append(tc, "duplicate", fmt.Sprintf("%.6f%%", o.duplicate/2))
		}
		if err := o.execute(ctx, "", "tc", tc...); err != nil {
			return err
		}
	}
	for _, e := range []struct {
		side, ns, addr, proxyAddr string
		netstack                  bool
	}{
		{"client", clientNS, o.addr(1), o.addr(2), o.client}, {"server", serverNS, o.addr(4), o.addr(3), o.server},
	} {
		dev := e.side + ".0"
		if err := ip("link", "set", "dev", dev, "netns", e.ns); err != nil {
			return err
		}
		// A Linux address at a netstack endpoint would send host-generated RSTs.
		if !e.netstack {
			if err := o.execute(ctx, e.ns, "ip", "addr", "add", e.proxyAddr+"/8", "dev", dev); err != nil {
				return err
			}
		}
		if err := o.execute(ctx, e.ns, "ip", "addr", "add", e.addr+"/8", "dev", dev); err != nil {
			return err
		}
		if o.disableGSO {
			if err := o.execute(ctx, e.ns, "ethtool", "-K", dev, "tso", "off", "gso", "off"); err != nil {
				return err
			}
		}
		if o.disableGRO {
			if err := o.execute(ctx, e.ns, "ethtool", "-K", dev, "gro", "off"); err != nil {
				return err
			}
		}
		for _, link := range []string{dev, "lo"} {
			if err := o.execute(ctx, e.ns, "ip", "link", "set", link, "up"); err != nil {
				return err
			}
		}
		if err := ip("link", "set", e.side+".1", "up"); err != nil {
			return err
		}
	}
	return nil
}
