// Copyright 2020 The gVisor Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//	http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
package network

import (
	"context"
	"flag"
	"os"
	"strconv"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/test/dockerutil"
	"gvisor.dev/gvisor/test/benchmarks/harness"
	"gvisor.dev/gvisor/test/benchmarks/tools"
	"gvisor.dev/gvisor/test/metricsviz"
)

var (
	nodeWarmupRequests = flag.Int("node-warmup-requests", 0, "requests to warm each actual Node server before measurement")
	nodeRedisCPUs      = flag.String("node-redis-cpuset", "", "explicit CPU affinity for the native Redis backend")
)

// BenchmarkNode runs requests using 'hey' against a Node server run on
// 'runtime'. The server responds to requests by grabbing some data in a
// redis instance and returns the data in its response. The test loops through
// increasing amounts of concurrency for requests.
func BenchmarkNode(b *testing.B) {
	harness.StartWorker(b)
	concurrency := []int{1, 5, 10, 25}
	for _, c := range concurrency {
		param := tools.Parameter{
			Name:  "concurrency",
			Value: strconv.Itoa(c),
		}
		name, err := tools.ParametersToName(param)
		if err != nil {
			b.Fatalf("Failed to parse parameters: %v", err)
		}
		b.Run(name, func(b *testing.B) {
			hey := &tools.Hey{
				Requests:    b.N,
				Concurrency: c,
			}
			runNode(b, hey)
		})
	}
}

// runNode runs the test for a given # of requests and concurrency.
func runNode(b *testing.B, hey *tools.Hey) {
	b.Helper()

	// The machine to hold Redis and the Node Server.
	serverMachine, err := harness.GetMachine()
	if err != nil {
		b.Fatalf("failed to get machine with: %v", err)
	}
	defer serverMachine.CleanUp()

	// The machine to run 'hey'.
	clientMachine, err := harness.GetMachine()
	if err != nil {
		b.Fatalf("failed to get machine with: %v", err)
	}
	defer clientMachine.CleanUp()

	ctx := context.Background()

	// Spawn a redis instance for the app to use.
	redis := serverMachine.GetNativeContainer(ctx, b)
	if err := redis.Spawn(ctx, dockerutil.RunOpts{
		Image:      "benchmarks/redis",
		CpusetCpus: *nodeRedisCPUs,
	}); err != nil {
		b.Fatalf("failed to spawn redis instance: %v", err)
	}
	defer redis.CleanUp(ctx)
	defer metricsviz.FromContainerLogs(ctx, b, redis)

	if out, err := redis.WaitForOutput(ctx, "Ready to accept connections", 3*time.Second); err != nil {
		b.Fatalf("failed to start redis server: %v %s", err, out)
	}
	redisIP, err := redis.FindIP(ctx, false)
	if err != nil {
		b.Fatalf("failed to get IP from redis instance: %v", err)
	}

	// Node runs on port 8080.
	port := 8080

	// Start-up the Node server.
	nodeApp := serverMachine.GetContainer(ctx, b)
	if err := nodeApp.Spawn(ctx, dockerutil.RunOpts{
		Image:      "benchmarks/node",
		WorkDir:    "/usr/src/app",
		Links:      []string{redis.MakeLink("redis")},
		CpusetCpus: harness.ServerCPUSet(),
	}, "node", "index.js", redisIP.String()); err != nil {
		b.Fatalf("failed to spawn node instance: %v", err)
	}
	defer nodeApp.CleanUp(ctx)

	// Wait until the Client sees the server as up.
	if err := harness.WaitUntilContainerServing(ctx, clientMachine, nodeApp, port); err != nil {
		b.Fatalf("server readiness failed: %v", err)
	}

	opts := dockerutil.RunOpts{
		Image:      "benchmarks/hey",
		Links:      []string{nodeApp.MakeLink("node")},
		CpusetCpus: harness.ClientCPUSet(),
	}
	if *nodeWarmupRequests > 0 {
		warmup := *hey
		warmup.Requests = *nodeWarmupRequests
		warmupClient := clientMachine.GetNativeContainer(ctx, b)
		defer warmupClient.CleanUp(ctx)
		// Warm the same server process whose requests will be measured.
		// The final runHey resets timing and replaces the warmup metrics.
		runHey(b, ctx, warmupClient, opts, &warmup, "warmup", []*dockerutil.Container{nodeApp, redis}, warmup.MakeCmd("node", port)...)
		// ResetTimer preserves timer state; restore the running setup timer
		// before the final runHey resets it for measured requests.
		b.StartTimer()
	}

	// The client runs on native runc.
	client := clientMachine.GetNativeContainer(ctx, b)
	defer client.CleanUp(ctx)
	runHey(b, ctx, client, opts, hey, "measured", []*dockerutil.Container{nodeApp, redis}, hey.MakeCmd("node", port)...)
}

func TestMain(m *testing.M) {
	harness.Init()
	os.Exit(m.Run())
}
