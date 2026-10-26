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

package hello

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/test/dockerutil"
	"gvisor.dev/gvisor/pkg/test/testutil"
	"gvisor.dev/gvisor/test/kubernetes/k8sctx"
	"gvisor.dev/gvisor/test/kubernetes/testcluster"
	nodev1 "k8s.io/api/node/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/clientcmd"
)

var (
	kindPath      = flag.String("kind", "", "declared kind executable")
	alpineArchive = flag.String("alpine-archive", "", "declared Alpine image archive to import into kind")
)

const kindNodeImage = "gvisor.dev/images/kubernetes/node:latest"
const kindAlpineImage = "gvisor.dev/images/basic/alpine:latest"

func TestMain(m *testing.M) {
	flag.Parse()
	os.Exit(dockerutil.RunTests(m.Run))
}

func kindCommand(ctx context.Context, t *testing.T, executable string, args ...string) error {
	t.Helper()
	cmd := exec.CommandContext(ctx, executable, args...)
	output, err := cmd.CombinedOutput()
	t.Logf("%s %q:\n%s", executable, args, output)
	if err != nil {
		return fmt.Errorf("%s %q: %w", executable, args, err)
	}
	return nil
}

// TestKindHello runs the maintained Kubernetes smoke test on a private cluster.
// The outer nodes use native runc; the RuntimeClass selects the declared runsc.
func TestKindHello(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), 15*time.Minute)
	defer cancel()
	if *kindPath == "" || *alpineArchive == "" {
		t.Fatal("kind and Alpine archive must be declared")
	}
	kind, err := filepath.Abs(*kindPath)
	if err != nil {
		t.Fatal(err)
	}
	archive, err := filepath.Abs(*alpineArchive)
	if err != nil {
		t.Fatal(err)
	}
	runtime, err := dockerutil.NamedRuntime("runsc")
	if err != nil {
		t.Fatal(err)
	}
	if runtime.Path == "" {
		t.Fatal("declared runsc registration has no executable")
	}
	runsc, err := filepath.EvalSymlinks(runtime.Path)
	if err != nil {
		t.Fatal(err)
	}
	// The release stages the shim and strict sidecars beside its executable.
	release := filepath.Dir(runsc)
	for _, name := range []string{"runsc", "containerd-shim-runsc-v1", "gvisor-bin"} {
		if _, err := os.Stat(filepath.Join(release, name)); err != nil {
			t.Fatalf("inspect declared release component %s: %v", name, err)
		}
	}

	work := t.TempDir()
	name := strings.ToLower(testutil.RandomID("gvisor"))
	node := name + "-control-plane"
	kubeconfig := filepath.Join(work, "kubeconfig")
	// Keep kind on the fixture's Docker daemon and its normal bridge/CNI. No
	// caller-selected provider or pre-existing cluster may substitute for it.
	t.Setenv("KIND_EXPERIMENTAL_PROVIDER", "docker")
	t.Setenv("KIND_EXPERIMENTAL_DOCKER_NETWORK", "")
	t.Setenv("KUBECONFIG", kubeconfig)
	logDir := os.Getenv("TEST_UNDECLARED_OUTPUTS_DIR")
	if logDir == "" {
		logDir = work
	}
	// Deletion also covers a partially created cluster. It has a fresh deadline
	// because test/setup cancellation must not prevent resource cleanup.
	t.Cleanup(func() {
		if t.Failed() {
			logCtx, logCancel := context.WithTimeout(context.Background(), 30*time.Second)
			if err := kindCommand(logCtx, t, kind, "export", "logs", "--name", name, filepath.Join(logDir, name)); err != nil {
				t.Errorf("export kind logs: %v", err)
			}
			logCancel()
		}
		cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), 2*time.Minute)
		defer cleanupCancel()
		if err := kindCommand(cleanupCtx, t, kind, "delete", "cluster", "--name", name); err != nil {
			t.Errorf("delete kind cluster: %v", err)
		}
	})
	if err := kindCommand(ctx, t, kind, "version"); err != nil {
		t.Fatal(err)
	}
	// kind otherwise pulls a missing node image. The shared fixture must have
	// loaded this declared archive before cluster creation.
	if err := kindCommand(ctx, t, "docker", "image", "inspect", "--format={{.Id}}", kindNodeImage); err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(work, "kind.yaml")
	const config = `kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
containerdConfigPatches:
- |-
  [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]
    runtime_type = "io.containerd.runsc.v1"
  [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc.options]
    TypeUrl = "io.containerd.runsc.v1.options"
    ConfigPath = "/etc/containerd/runsc.toml"
`
	if err := os.WriteFile(configPath, []byte(config), 0600); err != nil {
		t.Fatal(err)
	}
	// Keep failed nodes until our cleanup can export their boot logs and delete
	// them. kind's default failure cleanup would discard that evidence first.
	if err := kindCommand(ctx, t, kind, "create", "cluster", "--name", name, "--image", kindNodeImage, "--config", configPath, "--kubeconfig", kubeconfig, "--wait", "5m", "--retain"); err != nil {
		t.Fatal(err)
	}
	for _, component := range []string{"runsc", "containerd-shim-runsc-v1", "gvisor-bin"} {
		if err := kindCommand(ctx, t, "docker", "cp", filepath.Join(release, component), node+":/usr/local/bin/"); err != nil {
			t.Fatal(err)
		}
	}
	// These are node-local paths; Docker's shared-root/debug paths belong to
	// the outer daemon and must not be copied into the nested runtime config.
	runscConfig := `[runsc_config]
  debug = "true"
  debug-log = "/var/log/runsc/%ID%/gvisor.%COMMAND%.log"
`
	if source, ok := testutil.RuntimeTestClockSource(); ok {
		runscConfig += fmt.Sprintf("  clock-source = %q\n", source.String())
	}
	runscConfigPath := filepath.Join(work, "runsc.toml")
	if err := os.WriteFile(runscConfigPath, []byte(runscConfig), 0600); err != nil {
		t.Fatal(err)
	}
	if err := kindCommand(ctx, t, "docker", "cp", runscConfigPath, node+":/etc/containerd/runsc.toml"); err != nil {
		t.Fatal(err)
	}
	if err := kindCommand(ctx, t, "docker", "exec", node, "mkdir", "-p", "/var/log/runsc"); err != nil {
		t.Fatal(err)
	}
	if err := kindCommand(ctx, t, "docker", "exec", node, "systemctl", "restart", "containerd"); err != nil {
		t.Fatal(err)
	}
	if err := kindCommand(ctx, t, kind, "load", "image-archive", archive, "--name", name); err != nil {
		t.Fatal(err)
	}
	cfg, err := clientcmd.BuildConfigFromFlags("", kubeconfig)
	if err != nil {
		t.Fatal(err)
	}
	client, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		t.Fatal(err)
	}
	cluster := testcluster.NewTestClusterFromClient(name, client)
	cluster.SetContainerImages(map[string]string{"alpine": kindAlpineImage})
	cluster.OverrideTestNodepoolRuntime(testcluster.RuntimeTypeGVisor)
	if _, err := cluster.CreateRuntimeClass(ctx, &nodev1.RuntimeClass{
		ObjectMeta: metav1.ObjectMeta{Name: "gvisor"},
		Handler:    "runsc",
	}); err != nil {
		t.Fatal(err)
	}
	n, err := client.CoreV1().Nodes().Get(ctx, node, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if n.Labels == nil {
		n.Labels = make(map[string]string)
	}
	for key, value := range map[string]string{
		"model":                            "e2e",
		"nodepool-type":                    "test-runtime-nodepool",
		"runtime":                          "gvisor",
		"sandbox.gke.io/runtime":           "gvisor",
		"node.kubernetes.io/instance-type": "n2-standard-4",
	} {
		n.Labels[key] = value
	}
	if _, err := client.CoreV1().Nodes().Update(ctx, n, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	if err := cluster.SanityCheck(ctx); err != nil {
		t.Fatalf("native cluster sanity check: %v", err)
	}
	RunHello(ctx, t, k8sctx.New(cluster), cluster)
}
