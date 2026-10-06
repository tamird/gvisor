// Copyright 2025 The gVisor Authors.
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

package nftables

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"

	"gvisor.dev/gvisor/pkg/log"
	"gvisor.dev/gvisor/pkg/test/dockerutil"
	"gvisor.dev/gvisor/test/netutils"
)

// singleTest runs a TestCase. Each test follows a pattern:
//   - Create a container.
//   - Get the container's IP.
//   - Send the container our IP.
//   - Start a new goroutine running the local action of the test.
//   - Wait for both the container and local actions to finish.
//
// Container output is logged to $TEST_UNDECLARED_OUTPUTS_DIR if it exists, or
// to stderr.
func singleTest(t *testing.T, test TestCase) {
	for _, tc := range []bool{false, true} {
		subtest := "IPv4"
		if tc {
			subtest = "IPv6"
		}
		t.Run(test.Name()+"_"+subtest, func(t *testing.T) {
			nftablesTest(t, test, tc)
		})
	}
}

func nftablesTest(t *testing.T, test TestCase, ipv6 bool) {
	if _, ok := Tests[test.Name()]; !ok {
		log.Infof("no test found with name %q. Has it been registered?", test.Name())
		t.FailNow()
	}

	// Wait for the local and container goroutines to finish.
	var wg sync.WaitGroup
	defer wg.Wait()

	ctx, cancel := context.WithTimeout(t.Context(), test.Timeout())
	defer cancel()

	d := dockerutil.MakeContainer(ctx, t)
	// Testing cancels t.Context before cleanup. Give logging and removal
	// independent deadlines so a log retrieval timeout does not prevent removal.
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.WithoutCancel(t.Context()), test.Timeout())
		defer cancel()
		d.CleanUp(ctx)
	})
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.WithoutCancel(t.Context()), test.Timeout())
		defer cancel()
		if logs, err := d.Logs(ctx); err != nil {
			log.Infof("Failed to retrieve container logs: %v", err)
		} else {
			log.Infof("=== Container logs: ===\n%s\n", logs)
		}
	})

	// Create and start the container.
	opts := dockerutil.RunOpts{
		Image:      "nftables",
		CapAdd:     []string{"NET_ADMIN", "SYS_ADMIN"},
		Privileged: true,
	}
	d.CopyFiles(&opts, "/runner", "test/nftables/runner/runner")
	args := []string{"/runner/runner", "-name", test.Name()}
	if ipv6 {
		args = append(args, "-ipv6")
	}
	if err := d.Spawn(ctx, opts, args...); err != nil {
		log.Infof("docker run failed: %v", err)
		t.FailNow()
	}

	// Get the container IP.
	ip, err := d.FindIP(ctx, ipv6)
	if err != nil {
		// If ipv6 is not configured, don't fail.
		if ipv6 && err == dockerutil.ErrNoIP {
			log.Infof("No ipv6 address is available.")
			t.Skip()
		}
		log.Infof("failed to get container IP: %v", err)
		t.FailNow()
	}

	// The container learns our IP from the connection's source address;
	// ConnectTCP closes the connection without sending a payload.
	if err := netutils.ConnectTCP(ctx, ip, IPExchangePort, ipv6); err != nil {
		log.Infof("failed to send IP to container: %v", err)
		t.FailNow()
	}

	// Drop checks need their full observation window; slow container setup
	// may have consumed most of the setup context's deadline.
	cancel()
	ctx, cancel = context.WithTimeout(t.Context(), test.Timeout())
	defer cancel()

	// Run our side of the test.
	errCh := make(chan error, 2)
	wg.Add(1)
	go func() {
		defer wg.Done()
		if err := test.LocalAction(ctx, ip, ipv6); err != nil && !errors.Is(err, context.Canceled) {
			errCh <- fmt.Errorf("LocalAction failed: %v", err)
		} else {
			errCh <- nil
		}
		if test.LocalSufficient() {
			errCh <- nil
		}
	}()

	// Run the container side.
	wg.Add(1)
	go func() {
		defer wg.Done()
		// Wait for the final statement. This structure has the side
		// effect that all container logs will appear within the
		// individual test context.
		if _, err := d.WaitForOutput(ctx, TerminalStatement, test.Timeout()); err != nil && !errors.Is(err, context.Canceled) {
			errCh <- fmt.Errorf("ContainerAction timeout within: %v or failed: %v", test.Timeout(), err)
		} else {
			errCh <- nil
		}
		if test.ContainerSufficient() {
			errCh <- nil
		}
	}()

	for i := 0; i < 2; i++ {
		if err := <-errCh; err != nil {
			t.Error(err)
		}
	}
}

func TestFilterInputDropAll(t *testing.T) {
	singleTest(t, &FilterInputDropAll{})
}

func TestNftablesValidation(t *testing.T) {
	for _, tc := range validationTests {
		singleTest(t, tc)
	}
}
