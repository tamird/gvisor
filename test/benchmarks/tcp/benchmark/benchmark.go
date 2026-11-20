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
	"strconv"
	"strings"
	"syscall"
	"time"

	"gvisor.dev/gvisor/pkg/abi/linux"
)

// Allow proxies to finish profiles before forcibly reaping a stuck helper.
const (
	shutdownGrace        = 10 * time.Second
	serverStartupTimeout = 10 * time.Second
	proxyStartupTimeout  = 60 * time.Second
)

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
		// Unlink the private bridge ports before namespace teardown.
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

	ctx, cancel := context.WithCancelCause(parent)
	var children []chan struct{}
	defer func() {
		cancel(nil)
		for _, done := range children {
			<-done
		}
	}()
	start := func(ns, name string, args []string, sig os.Signal) (int, error) {
		cmd := o.command(ctx, ns, args[0], args[1:]...)
		cmd.Cancel = func() error { return cmd.Process.Signal(sig) }
		cmd.WaitDelay = shutdownGrace
		if err := cmd.Start(); err != nil {
			if ctx.Err() != nil {
				return 0, context.Cause(ctx)
			}
			return 0, fmt.Errorf("start %s: %w", name, err)
		}
		done := make(chan struct{})
		children = append(children, done)
		go func() {
			_ = cmd.Wait()
			cancel(fmt.Errorf("%s exited unexpectedly: %v", name, cmd.ProcessState))
			close(done)
		}()
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
	if err := o.waitListener(ctx, serverNS, iperfPort, serverStartupTimeout); err != nil {
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
	if err := o.waitListener(ctx, clientNS, proxyPort, proxyStartupTimeout); err != nil {
		return fmt.Errorf("client proxy failed to listen: %w", err)
	}
	args := append(iperf[1:], "-J", "-p", proxyPort, "-c", o.addr(1), "-t", strconv.Itoa(o.duration), "-f", "m", "-P", strconv.Itoa(o.streams))
	b, err := o.clientOutput(ctx, clientNS, args, time.Duration(o.duration+60)*time.Second)
	_, _ = os.Stderr.Write(b)
	if err != nil {
		if ctx.Err() != nil {
			return context.Cause(ctx)
		}
		return err
	}
	if ctx.Err() != nil {
		return context.Cause(ctx)
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
	_, _ = fmt.Printf("BenchmarkTCP/role=%s/host-gso=%t/host-gro=%t 1 %s Mb/s %.12g cpu-time\n", role, !o.disableGSO, !o.disableGRO, strconv.FormatFloat(rate, 'f', -1, 64), cpu)
	return nil
}

// clientOutput bounds the complete transfer, including iperf's control exchange.
func (o *options) clientOutput(parent context.Context, ns string, args []string, timeout time.Duration) ([]byte, error) {
	ctx, cancel := context.WithTimeout(parent, timeout)
	defer cancel()
	cmd := o.command(ctx, ns, o.iperf, args...)
	cmd.Cancel = func() error { return cmd.Process.Signal(syscall.SIGTERM) }
	cmd.WaitDelay = shutdownGrace
	cmd.Stdout = nil
	b, err := cmd.Output()
	if ctx.Err() != nil {
		return b, fmt.Errorf("iperf client did not finish within %s: %w", timeout, context.Cause(ctx))
	}
	return b, err
}
