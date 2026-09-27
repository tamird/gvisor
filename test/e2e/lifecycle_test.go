// Copyright 2018 The gVisor Authors.
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

package integration

import (
	"context"
	"flag"
	"fmt"
	"net/http"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/test/dockerutil"
	"gvisor.dev/gvisor/pkg/test/testutil"
)

// defaultWait is the default wait time used for tests.
const defaultWait = time.Minute

// The ordinary integration target uses its preconfigured daemon. The focused
// source qualification target supplies a declared image to its private daemon.
var dockerImageArchive = flag.String("docker_image_archive", "", "start a private Docker daemon and load this image archive")

// httpRequestSucceeds sends a request to a given url and checks that the status is OK.
func httpRequestSucceeds(client http.Client, server string, port int) error {
	url := fmt.Sprintf("http://%s:%d", server, port)
	// Ensure that content is being served.
	resp, err := client.Get(url)
	if err != nil {
		return fmt.Errorf("error reaching http server: %v", err)
	}
	defer resp.Body.Close()
	if want := http.StatusOK; resp.StatusCode != want {
		return fmt.Errorf("wrong response code, got: %d, want: %d", resp.StatusCode, want)
	}
	return nil
}

// TestLifeCycle tests a basic Create/Start/Stop docker container life cycle.
func TestLifeCycle(t *testing.T) {
	if *dockerImageArchive != "" {
		runsc, err := testutil.FindFile("release/runsc")
		if err != nil {
			t.Fatal(err)
		}
		dockerutil.StartDaemon(t, runsc, *dockerImageArchive)
	}
	ctx := context.Background()
	d := dockerutil.MakeContainer(ctx, t)
	defer d.CleanUp(ctx)

	// Start the container.
	port := 80
	if err := d.Create(ctx, dockerutil.RunOpts{
		Image: "basic/nginx",
	}); err != nil {
		t.Fatalf("docker create failed: %v", err)
	}
	if err := d.Start(ctx); err != nil {
		t.Fatalf("docker start failed: %v", err)
	}

	ip, err := d.FindIP(ctx, false)
	if err != nil {
		t.Fatalf("docker.FindIP failed: %v", err)
	}
	if err := testutil.WaitForHTTP(ip.String(), port, defaultWait); err != nil {
		t.Fatalf("WaitForHTTP() timeout: %v", err)
	}
	client := http.Client{Timeout: defaultWait}
	if err := httpRequestSucceeds(client, ip.String(), port); err != nil {
		t.Errorf("http request failed: %v", err)
	}

	if err := d.Stop(ctx); err != nil {
		t.Fatalf("docker stop failed: %v", err)
	}
	if err := d.Remove(ctx); err != nil {
		t.Fatalf("docker rm failed: %v", err)
	}
}
