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
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"

	"golang.org/x/sys/unix"

	"gvisor.dev/gvisor/pkg/abi/linux"
)

// Allow proxies to finish profiles before forcibly reaping a stuck helper.
const shutdownGrace = 10 * time.Second

// senderRate returns iperf3's final sender aggregate in Mbit/s. Individual
// stream measurements do not describe total throughput for parallel clients.
func senderRate(b []byte) (float64, error) {
	var result struct {
		Error string
		End   struct {
			Sent struct {
				BPS *float64 `json:"bits_per_second"`
			} `json:"sum_sent"`
		}
	}
	if err := json.Unmarshal(b, &result); err != nil {
		return 0, fmt.Errorf("reading iperf result: %w", err)
	}
	if result.Error != "" {
		return 0, fmt.Errorf("iperf: %s", result.Error)
	}
	if result.End.Sent.BPS == nil || *result.End.Sent.BPS < 0 {
		return 0, fmt.Errorf("iperf did not report an aggregate sender rate")
	}
	return *result.End.Sent.BPS / 1e6, nil
}

// cpuLoad reads user and system ticks before stopping the proxy. Shutdown
// itself can consume CPU, for example when a proxy writes its profiles.
func cpuLoad(pid, duration int) (float64, error) {
	b, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	if err != nil {
		return 0, err
	}
	// The parenthesized executable name may itself contain spaces or ')'.
	end := bytes.LastIndexByte(b, ')')
	fields := strings.Fields(string(b[end+1:]))
	if end < 0 || len(fields) < 13 {
		return 0, fmt.Errorf("incomplete /proc/%d/stat", pid)
	}
	var ticks uint64
	for _, v := range fields[11:13] {
		n, err := strconv.ParseUint(v, 10, 64)
		if err != nil {
			return 0, err
		}
		ticks += n
	}
	return float64(ticks) / float64(linux.CLOCKS_PER_SEC) / float64(duration), nil
}

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
		_, _ = fmt.Fprintf(os.Stderr, "exec %q %q\n", program, args)
	}
	cmd := exec.CommandContext(ctx, program, args...)
	cmd.Stdout, cmd.Stderr = os.Stderr, os.Stderr
	return cmd
}

func (o *options) execute(ctx context.Context, ns, program string, args ...string) error {
	cmd := o.command(ctx, ns, program, args...)
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("%v: %w", cmd, err)
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
		tc := []string{
			"qdisc", "add", "dev", wan, "root", "netem", "delay",
			fmt.Sprintf("%.2fms", o.latency/2), fmt.Sprintf("%gms", o.jitter),
		}
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
	addressArgs := func(address, dev string) []string {
		args := []string{"addr", "add", address + "/8", "dev", dev}
		if o.ipv6 {
			args = append(args, "nodad")
		}
		return args
	}
	for _, e := range []struct {
		side, ns, addr, proxyAddr string
		netstack                  bool
	}{
		{"client", clientNS, o.addr(1), o.addr(2), o.client},
		{"server", serverNS, o.addr(4), o.addr(3), o.server},
	} {
		dev := e.side + ".0"
		if err := ip("link", "set", "dev", dev, "netns", e.ns); err != nil {
			return err
		}
		// A Linux address at a netstack endpoint would send host-generated RSTs.
		if !e.netstack {
			if err := o.execute(ctx, e.ns, "ip", addressArgs(e.proxyAddr, dev)...); err != nil {
				return err
			}
		}
		if err := o.execute(ctx, e.ns, "ip", addressArgs(e.addr, dev)...); err != nil {
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

func (o *options) waitListener(parent context.Context, ns, port string, timeout time.Duration) error {
	ctx, cancel := context.WithTimeout(parent, timeout)
	defer cancel()
	tick := time.NewTicker(100 * time.Millisecond)
	defer tick.Stop()
	for {
		// A connection would consume the first socket reserved for iperf.
		cmd := o.command(ctx, ns, "ss", "-Hltn", "sport = :"+port)
		cmd.Stdout = nil
		output, err := cmd.Output()
		if ctx.Err() != nil {
			return context.Cause(ctx)
		}
		if err != nil {
			return fmt.Errorf("inspect listener on port %s: %w", port, err)
		}
		if len(bytes.TrimSpace(output)) > 0 {
			return nil
		}
		select {
		case <-ctx.Done():
			return context.Cause(ctx)
		case <-tick.C:
		}
	}
}

func (o *options) benchmark(parent context.Context) error {
	client, err := newNetNS()
	if err != nil {
		return fmt.Errorf("create client network namespace: %w", err)
	}
	defer client.Close()
	server, err := newNetNS()
	if err != nil {
		return fmt.Errorf("create server network namespace: %w", err)
	}
	defer server.Close()
	defer func() {
		// Detach bridge ports before network namespace teardown.
		ctx, cancel := context.WithTimeout(context.Background(), shutdownGrace)
		defer cancel()
		for _, dev := range []string{"client.1", "server.1", "wan.0", "wan.1"} {
			_ = o.execute(ctx, "", "ip", "link", "set", dev, "nomaster")
		}
	}()
	clientNS := fmt.Sprintf("/proc/1/fd/%d", client.Fd())
	serverNS := fmt.Sprintf("/proc/1/fd/%d", server.Fd())
	if err := o.setup(parent, clientNS, serverNS); err != nil {
		return err
	}

	ctx, cancel := context.WithCancel(parent)
	var children []*exec.Cmd
	defer func() {
		cancel()
		for _, cmd := range children {
			_ = cmd.Wait()
		}
	}()
	start := func(ns, name string, args []string, sig os.Signal) (int, error) {
		cmd := o.command(ctx, ns, args[0], args[1:]...)
		cmd.Cancel = func() error { return cmd.Process.Signal(sig) }
		cmd.WaitDelay = shutdownGrace
		if err := cmd.Start(); err != nil {
			return 0, fmt.Errorf("start %s: %w", name, err)
		}
		children = append(children, cmd)
		return cmd.Process.Pid, nil
	}
	iperf := []string{o.iperf}
	if o.ipv6 {
		iperf = append(iperf, "-6")
	}
	if !o.client && !o.server {
		_ = o.execute(ctx, clientNS, "ping", "-c", "100", "-i", "0.001", "-W", "1", o.addr(4))
	}
	if _, err := start(serverNS, "iperf server", append(iperf, "-p", iperfPort, "-s"), os.Kill); err != nil {
		return err
	}
	if err := o.waitListener(ctx, serverNS, iperfPort, 10*time.Second); err != nil {
		return fmt.Errorf("iperf server failed to listen: %w", err)
	}
	serverPID, err := start(serverNS, "server proxy", o.proxyArgs("server"), syscall.SIGTERM)
	if err != nil {
		return err
	}
	clientPID, err := start(clientNS, "client proxy", o.proxyArgs("client"), syscall.SIGTERM)
	if err != nil {
		return err
	}
	if err := o.waitListener(ctx, clientNS, proxyPort, 60*time.Second); err != nil {
		return fmt.Errorf("client proxy failed to listen: %w", err)
	}
	args := append(iperf[1:],
		"-J", "-p", proxyPort, "-c", o.addr(1),
		"-t", strconv.Itoa(o.duration), "-f", "m", "-P", strconv.Itoa(o.streams),
	)
	transferTimeout := time.Duration(o.duration+60) * time.Second
	transferCtx, stop := context.WithTimeout(ctx, transferTimeout)
	defer stop()
	cmd := o.command(transferCtx, clientNS, o.iperf, args...)
	cmd.Cancel = func() error { return cmd.Process.Signal(syscall.SIGTERM) }
	cmd.WaitDelay = shutdownGrace
	cmd.Stdout = nil
	b, err := cmd.Output()
	_, _ = os.Stderr.Write(b)
	if ctx.Err() != nil {
		return ctx.Err()
	}
	if transferCtx.Err() != nil {
		return fmt.Errorf("iperf client did not finish within %s: %w", transferTimeout, context.Cause(transferCtx))
	}
	if err != nil {
		return err
	}
	rate, err := senderRate(b)
	if err != nil {
		return err
	}
	role, pid := "server", serverPID
	if o.client || o.linuxClient {
		role, pid = "client", clientPID
	}
	cpu, err := cpuLoad(pid, o.duration)
	if err != nil {
		return fmt.Errorf("CPU time for %s: %w", role, err)
	}
	_, _ = fmt.Printf("BenchmarkTCP/role=%s/host-gso=%t/host-gro=%t 1 %s Mb/s %.12g cpu-time\n",
		role, !o.disableGSO, !o.disableGRO, strconv.FormatFloat(rate, 'f', -1, 64), cpu)
	return nil
}
