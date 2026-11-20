// Copyright 2020 The gVisor Authors.
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

// Package harness holds utility code for running benchmarks on Docker.
package harness

import (
	"context"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"time"

	"gvisor.dev/gvisor/pkg/test/dockerutil"
	"gvisor.dev/gvisor/pkg/test/testutil"
)

var (
	containerObservationDir = flag.String("container-observation-dir", "", "wait outside benchmark timing for live container attestation")
	help           = flag.Bool("help", false, "print this usage message")
	ownedDocker    = flag.Bool("owned-docker", false, "start a private Docker worker from declared inputs")
	workerRunsc    = flag.String("worker-runsc", "", "entrypoint in the declared complete runsc release; empty selects native runc")
	workerArchives = flag.String("worker-image-archives", "", "comma-separated declared Docker-save archives")
	clientCPUs     = flag.String("client-cpuset", "", "explicit benchmark client CPU affinity; empty selects the default split")
	serverCPUs     = flag.String("server-cpuset", "", "explicit benchmark server CPU affinity; empty selects the default split")
	debug          = flag.Bool("debug", false, "turns on debug messages for individual benchmarks")
)

// Init performs any harness initialization before runs.
func Init() error {
	flag.Usage = func() {
		fmt.Fprintf(os.Stderr, "Usage: %s -- --test.bench=<regex>\n", os.Args[0])
		flag.PrintDefaults()
	}
	flag.Parse()
	if *help {
		flag.Usage()
		os.Exit(0)
	}
	if !*ownedDocker {
		dockerutil.EnsureSupportedDockerVersion()
	}
	return nil
}

// SetFixedBenchmarks causes all benchmarks to run once.
//
// This must be set if they cannot scale with N. Note that this uses 1ns
// instead of 1x due to https://github.com/golang/go/issues/32051.
func SetFixedBenchmarks() {
	flag.Set("test.benchtime", "1ns")
}

// GetMachine returns this run's implementation of machine.
func GetMachine() (Machine, error) {
	return &localMachine{}, nil
}

// ClientCPUSet returns the CPU affinity string for benchmark client containers.
func ClientCPUSet() string {
	if *clientCPUs != "" {
		return *clientCPUs
	}
	if n := runtime.NumCPU(); n >= 8 {
		return fmt.Sprintf("0-%d", n/2-1)
	} else if n >= 4 {
		return "0-1"
	}
	return ""
}

// ServerCPUSet returns the CPU affinity string for benchmark server containers.
func ServerCPUSet() string {
	if *serverCPUs != "" {
		return *serverCPUs
	}
	if n := runtime.NumCPU(); n >= 8 {
		return fmt.Sprintf("%d-%d", n/2, n-1)
	} else if n >= 4 {
		return "2-3"
	}
	return ""
}

// AwaitContainerObservation keeps the container alive until its current process
// has been inspected. It must be called with the benchmark timer stopped.
func AwaitContainerObservation(ctx context.Context, c *dockerutil.Container) error {
	if *containerObservationDir == "" {
		return nil
	}
	state, err := c.Status(ctx)
	if err != nil {
		return err
	}
	if !state.Running || state.Pid <= 0 {
		return fmt.Errorf("container %s has no live process to attest", c.ID())
	}
	ack := filepath.Join(*containerObservationDir, fmt.Sprintf("%s-%d", c.ID(), state.Pid))
	return testutil.Poll(func() error {
		_, err := os.Stat(ack)
		return err
	}, 5*time.Second)
}
