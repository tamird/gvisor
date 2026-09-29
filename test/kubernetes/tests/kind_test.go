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

// diagnoseKindNetfilter is a fork-only comparison using a fresh endpoint on the
// failed node's Docker network. It does not inspect the stopped node's namespace.
func diagnoseKindNetfilter(t *testing.T, node string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	output, err := exec.CommandContext(ctx, "docker", "inspect", "--format={{.HostConfig.NetworkMode}}", node).CombinedOutput()
	if err != nil {
		t.Errorf("inspect failed node network: %v: %s", err, output)
		return
	}
	network := strings.TrimSpace(string(output))
	if network != "kind" {
		t.Errorf("diagnostic requires the actual kind network, got %q", network)
		return
	}
	t.Logf("Netfilter diagnostic: fresh native endpoint on network %q, image %q", network, kindNodeImage)
	// Docker invokes the generic iptables executable from the outer PATH.
	// Keep that path when executing: resolving a multicall symlink can change
	// argv[0] dispatch. The resolved path is evidence only.
	tools := make(map[string]string)
	for _, tool := range []string{"iptables", "iptables-save", "nsenter"} {
		path, err := exec.LookPath(tool)
		if err != nil {
			t.Errorf("find host diagnostic tool %s: %v", tool, err)
			continue
		}
		tools[tool] = path
		resolved, err := filepath.EvalSymlinks(path)
		if err != nil {
			t.Errorf("resolve host diagnostic tool %s: %v", path, err)
		}
		t.Logf("Host diagnostic tool %s: path=%q resolved=%q", tool, path, resolved)
		if err := kindCommand(ctx, t, path, "--version"); err != nil {
			t.Errorf("host diagnostic version: %v", err)
		}
	}
	d := dockerutil.MakeNativeContainer(ctx, t)
	if d == nil {
		t.Error("create native diagnostic client")
		return
	}
	defer func() {
		cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cleanupCancel()
		if err := d.CleanUp(cleanupCtx); err != nil {
			t.Errorf("clean up netfilter diagnostic container: %v", err)
		}
	}()
	const commands = `set -u
status=0
errors=$(mktemp)
run() {
  printf '\nBEGIN stdout: %s\n' "$*"
  if timeout 5 "$@" 2>"$errors"; then rc=0; else rc=$?; status=1; fi
  printf '\nEND stdout: status=%s\nBEGIN stderr\n' "$rc"
  cat "$errors"
  printf '\nEND stderr\n'
}
run uname -a
run cat /etc/resolv.conf
run grep -E '^nameserver[[:space:]]+127\.0\.0\.11([[:space:]]|$)' /etc/resolv.conf
run cat /proc/net/ip_tables_names /proc/net/ip_tables_matches /proc/net/ip_tables_targets
for tool in iptables-legacy ip6tables-legacy iptables-nft ip6tables-nft; do
  run "$tool" --version
  run "$tool-save"
done
rm -f "$errors"
exit "$status"
`
	// Keep this endpoint alive while both userspaces read its rules. Only the
	// network namespace is entered, so nsenter uses the outer executable and
	// libraries. Deferred cleanup owns termination; sleep bounds its lifetime.
	if err := d.Spawn(ctx, dockerutil.RunOpts{
		Image:        "kubernetes/node:latest",
		Entrypoint:   []string{"/bin/sh"},
		Privileged:   true,
		NetworkMode:  network,
		CgroupnsMode: "private",
	}, "-c", "exec sleep 120"); err != nil {
		t.Errorf("start native diagnostic endpoint: %v", err)
		return
	}
	state, err := d.Status(ctx)
	if err != nil {
		t.Errorf("inspect native diagnostic endpoint: %v", err)
		return
	}
	if !state.Running || state.Pid <= 0 {
		t.Errorf("diagnostic endpoint is not running: %+v", state)
		return
	}
	t.Logf("Comparing netfilter readers on endpoint %s, host PID %d", d.ID(), state.Pid)
	if tools["nsenter"] != "" && tools["iptables-save"] != "" {
		if err := kindCommand(ctx, t, tools["nsenter"], "--target", fmt.Sprint(state.Pid), "--net", "--", tools["iptables-save"]); err != nil {
			t.Errorf("save endpoint rules with host iptables: %v", err)
		}
	}
	// The Docker CLI captures partial output even if its context expires. Both
	// readers run while the endpoint is alive; no rule is restored or modified.
	if err := kindCommand(ctx, t, "docker", "exec", d.ID(), "/bin/sh", "-c", commands); err != nil {
		t.Errorf("read endpoint rules with node tools: %v", err)
	}
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
	t.Setenv("UNSANDBOXED_RUNTIME", "runc")
	logDir := os.Getenv("TEST_UNDECLARED_OUTPUTS_DIR")
	if logDir == "" {
		logDir = work
	}
	// Deletion also covers a partially created cluster. It has a fresh deadline
	// because test/setup cancellation must not prevent resource cleanup.
	t.Cleanup(func() {
		if t.Failed() {
			diagnoseKindNetfilter(t, node)
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
	const runscConfig = `[runsc_config]
  debug = "true"
  debug-log = "/var/log/runsc/%ID%/gvisor.%COMMAND%.log"
  sidecar-usage-policy = "STRICT"
`
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
