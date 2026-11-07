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

// Binary tcp_benchmark measures throughput between two TCP proxies.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strconv"
	"syscall"

	"golang.org/x/sys/unix"
)

const (
	proxyPort = "44000"
	iperfPort = "45201"
)

type options struct {
	client, server, linuxClient, verbose, ipv6, noUserNS bool
	disableGSO, disableGRO, swgso, gro, xdp              bool
	mtu, gso, duration, streams                          int
	latency, jitter, loss, duplicate                     float64
	proxy, nsjoin, iperf, helpers                        string
	netstackArgs                                         []string
}

func parse(args []string) (*options, error) {
	o := new(options)
	f := flag.NewFlagSet("tcp_benchmark", flag.ContinueOnError)
	f.BoolVar(&o.client, "client", false, "use netstack on the client")
	f.BoolVar(&o.server, "server", false, "use netstack on the server")
	f.BoolVar(&o.linuxClient, "linux-client", false, "report the Linux client proxy's CPU usage")
	f.BoolVar(&o.verbose, "verbose", false, "print commands to stderr")
	f.BoolVar(&o.noUserNS, "no-user-ns", false, "use the current user namespace (for root)")
	f.BoolVar(&o.ipv6, "ipv6", false, "use IPv6")
	f.BoolVar(&o.disableGSO, "disable-linux-gso", false, "disable Linux TSO and GSO")
	f.BoolVar(&o.disableGRO, "disable-linux-gro", false, "disable Linux GRO")
	f.BoolVar(&o.swgso, "swgso", false, "use software GSO in netstack")
	f.BoolVar(&o.gro, "gro", false, "enable netstack GRO")
	f.BoolVar(&o.xdp, "xdp", false, "use AF_XDP instead of AF_PACKET")
	f.IntVar(&o.gso, "gso", 0, "netstack GSO maximum size")
	f.IntVar(&o.mtu, "mtu", 1280, "MTU in bytes")
	f.IntVar(&o.duration, "duration", 30, "iperf duration in seconds")
	f.IntVar(&o.streams, "num-client-threads", 1, "number of parallel iperf streams")
	f.Float64Var(&o.latency, "latency", 10, "round-trip delay in milliseconds")
	f.Float64Var(&o.jitter, "latency-variation", 1, "delay variation in milliseconds")
	f.Float64Var(&o.loss, "loss", 0.1, "packet loss percentage")
	f.Float64Var(&o.duplicate, "duplicate", 0.1, "packet duplication percentage")
	f.BoolFunc("ideal", "reset network emulation and set the MTU to 1500", func(string) error {
		o.mtu = 1500
		o.latency, o.jitter, o.loss, o.duplicate = 0, 0, 0, 0
		return nil
	})
	for name, proxyFlag := range map[string]string{"sack": "sack", "rack": "rack", "cubic": "cubic", "moderate-recv-buf": "moderate_recv_buf", "sniff": "sniff"} {
		f.BoolFunc(name, "enable the proxy's -"+proxyFlag+" option", func(v string) error {
			o.netstackArgs = append(o.netstackArgs, "-"+proxyFlag+"="+v)
			return nil
		})
	}
	for _, name := range []string{"client_tcp_probe_file", "server_tcp_probe_file", "cpuprofile", "memprofile", "blockprofile", "mutexprofile", "traceprofile"} {
		f.Func(name, "file for the proxy's "+name+" output", func(path string) error {
			o.netstackArgs = append(o.netstackArgs, "-"+name+"="+path)
			return nil
		})
	}
	program, err := exec.LookPath(os.Args[0])
	if err != nil {
		return nil, err
	}
	dir := filepath.Dir(program)
	f.StringVar(&o.helpers, "helpers", "", "directory containing tcp_proxy and nsjoin")
	f.StringVar(&o.proxy, "tcp-proxy", filepath.Join(dir, "tcp_proxy"), "tcp_proxy executable")
	f.StringVar(&o.nsjoin, "nsjoin", filepath.Join(dir, "nsjoin"), "namespace helper executable")
	f.StringVar(&o.iperf, "iperf-binary", "iperf3", "iperf3 executable")
	if err := f.Parse(args); err != nil {
		return nil, err
	}
	if f.NArg() != 0 {
		return nil, fmt.Errorf("unexpected argument %q", f.Arg(0))
	}
	if o.duration <= 0 || o.streams <= 0 {
		return nil, errors.New("duration and num-client-threads must be positive")
	}
	if o.helpers != "" {
		o.proxy = filepath.Join(o.helpers, "tcp_proxy")
		o.nsjoin = filepath.Join(o.helpers, "nsjoin")
	}
	for _, path := range []*string{&o.proxy, &o.nsjoin} {
		abs, err := filepath.Abs(*path)
		if err != nil {
			return nil, err
		}
		*path = abs
	}
	return o, nil
}

func (o *options) addr(n int) string {
	if o.ipv6 {
		return fmt.Sprintf("fd::%d", n)
	}
	return fmt.Sprintf("10.0.0.%d", n)
}

func (o *options) proxyArgs(side string) []string {
	forward := net.JoinHostPort(o.addr(4), iperfPort)
	address := o.addr(3)
	netstack := o.server
	if side == "client" {
		forward = net.JoinHostPort(o.addr(3), proxyPort)
		address = o.addr(2)
		netstack = o.client
	}
	args := []string{o.proxy, "-port", proxyPort, "-forward", forward}
	if !netstack {
		return args
	}
	args = append(args, o.netstackArgs...)
	return append(args, "-"+side, "-mtu", strconv.Itoa(o.mtu), "-iface", side+".0", "-addr", address, "-mask", "8",
		"-gso="+strconv.Itoa(o.gso), fmt.Sprintf("-swgso=%t", o.swgso), fmt.Sprintf("-gro=%t", o.gro), fmt.Sprintf("-xdp=%t", o.xdp), fmt.Sprintf("-ipv6=%t", o.ipv6))
}

func run(ctx context.Context, args []string) error {
	inside := len(args) > 0 && args[0] == "--inside-benchmark-namespace"
	if inside {
		args = args[1:]
	}
	o, err := parse(args)
	if err != nil {
		return err
	}
	if inside {
		// The private PID namespace makes the coordinator PID 1. Its children
		// cannot outlive it, including when the benchmark is interrupted.
		if os.Getpid() != 1 {
			return errors.New("benchmark namespace coordinator must be PID 1")
		}
		if err := unix.Mount("", "/", "", unix.MS_REC|unix.MS_PRIVATE, ""); err != nil {
			return fmt.Errorf("make mount propagation private: %w", err)
		}
		if err := unix.Mount("proc", "/proc", "proc", unix.MS_NOSUID|unix.MS_NODEV|unix.MS_NOEXEC, ""); err != nil {
			return fmt.Errorf("mount private proc: %w", err)
		}
		return o.benchmark(ctx)
	}
	cmd := exec.CommandContext(ctx, "/proc/self/exe", append([]string{"--inside-benchmark-namespace"}, args...)...)
	cmd.Args[0] = os.Args[0] // Keep sibling helper discovery when invoked outside Bazel.
	// Let PID 1 stop the proxies and flush their profiles. Killing PID 1
	// terminates any remaining processes if its cleanup stalls.
	cmd.Cancel = func() error { return cmd.Process.Signal(syscall.SIGTERM) }
	cmd.WaitDelay = 2 * shutdownGrace
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	attr := &syscall.SysProcAttr{Cloneflags: unix.CLONE_NEWNS | unix.CLONE_NEWPID | unix.CLONE_NEWNET, Pdeathsig: syscall.SIGKILL}
	if !o.noUserNS {
		attr.Cloneflags |= unix.CLONE_NEWUSER
		attr.UidMappings = []syscall.SysProcIDMap{{ContainerID: 0, HostID: os.Geteuid(), Size: 1}}
		attr.GidMappings = []syscall.SysProcIDMap{{ContainerID: 0, HostID: os.Getegid(), Size: 1}}
	}
	cmd.SysProcAttr = attr
	return cmd.Run()
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := run(ctx, os.Args[1:]); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return
		}
		var e *exec.ExitError
		if errors.As(err, &e) {
			if status, ok := e.Sys().(syscall.WaitStatus); ok && status.Signaled() {
				os.Exit(128 + int(status.Signal()))
			}
			if e.ExitCode() > 0 {
				os.Exit(e.ExitCode())
			}
		}
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
