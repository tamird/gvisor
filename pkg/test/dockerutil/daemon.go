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

package dockerutil

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/cenkalti/backoff"
	"github.com/docker/docker/client"
	specs "github.com/opencontainers/runtime-spec/specs-go"
	"github.com/vishvananda/netlink"
	"golang.org/x/sys/unix"

	"gvisor.dev/gvisor/pkg/test/testutil"
	"gvisor.dev/gvisor/runsc/cgroup"
	"gvisor.dev/gvisor/runsc/specutils"
)

var dockerTestConfig = flag.String("docker_test_config", "", "declared runtime and image inputs for a private Docker test daemon")

// daemonInputs is written by the Docker test configuration rule. All paths are
// relative to the test's runfiles root.
type daemonInputs struct {
	Runsc       string   `json:"runsc"`
	Native      bool     `json:"native"`
	Images      []string `json:"images"`
	RuntimeArgs []string `json:"runtime_args"`
	IPv6        bool     `json:"ipv6"`
}

// RunTests runs a Docker integration suite. Callers must parse flags first.
// Without --docker_test_config, it retains the installed-daemon interface.
// Otherwise setup precedes the version check, and cleanup follows all tests,
// including parallel tests. The run callback may initialize suite state from
// the selected daemon before calling m.Run. Only TestMain exits the process.
func RunTests(run func() int) (status int) {
	if *dockerTestConfig != "" {
		data, err := os.ReadFile(*dockerTestConfig)
		if err != nil {
			log.Printf("read Docker test configuration: %v", err)
			return 1
		}
		var inputs daemonInputs
		if err := json.Unmarshal(data, &inputs); err != nil {
			log.Printf("decode Docker test configuration: %v", err)
			return 1
		}
		if inputs.Native && (inputs.Runsc != "" || len(inputs.RuntimeArgs) != 0) {
			log.Print("Native Docker configuration cannot declare runsc or its arguments")
			return 1
		}
		if !inputs.Native && inputs.Runsc == "" {
			log.Print("Docker test configuration must declare a runtime or select native mode")
			return 1
		}
		d := &testDaemon{}
		defer func() {
			if err := d.close(status != 0); err != nil {
				log.Printf("clean up private Docker daemon: %v", err)
				if status == 0 {
					status = 1
				}
			}
		}()
		if err := d.start(inputs); err != nil {
			log.Printf("start private Docker daemon: %v", err)
			return 1
		}
	}
	if err := checkSupportedDockerVersion(); err != nil {
		log.Printf("check Docker version: %v", err)
		return 1
	}
	return run()
}

// testDaemon owns suite-wide environment and runtime state. It is initialized
// before m.Run and closed after m.Run, never concurrently with a test.
type testDaemon struct {
	root       string
	dataRoot   string
	execRoot   string
	logs       *os.File
	cmd        *exec.Cmd
	done       chan struct{}
	waitErr    error
	env        map[string]*string
	oldRuntime string
	oldConfig  string
	configured bool
	cgroup     cgroup.Cgroup
}

func (d *testDaemon) start(inputs daemonInputs) error {
	runtimeName := *runtime
	var runsc, sidecars string
	var err error
	if inputs.Native {
		if runtimeName != "" && runtimeName != "runc" {
			return fmt.Errorf("native Docker configuration requires runc, got %q", runtimeName)
		}
		runtimeName = "runc"
	} else {
		if runtimeName == "" {
			runtimeName = "runsc"
		}
		runsc, err = filepath.Abs(inputs.Runsc)
		if err != nil {
			return err
		}
		sidecars = filepath.Join(filepath.Dir(runsc), "gvisor-bin")
		st, err := os.Stat(sidecars)
		if err != nil {
			return fmt.Errorf("inspect declared sidecars at %q: %w", sidecars, err)
		}
		if !st.IsDir() {
			return fmt.Errorf("declared sidecar path %q is not a directory", sidecars)
		}
	}
	// Both dockerd and containerd put Unix sockets below this directory. Bazel's
	// TMPDIR can exceed sockaddr_un's path limit, so keep the owned root short.
	d.root, err = os.MkdirTemp("/tmp", "gvisor-docker-")
	if err != nil {
		return err
	}
	d.execRoot = filepath.Join(d.root, "exec")
	// Firecracker sizes its root disk from EstimatedFreeDiskBytes; the action
	// workspace only has fixed writable slack beyond its declared inputs.
	d.dataRoot = filepath.Join(d.root, "data")
	if err := os.Mkdir(d.dataRoot, 0700); err != nil {
		return err
	}
	logDir := os.Getenv("TEST_UNDECLARED_OUTPUTS_DIR")
	if logDir == "" {
		logDir = d.root
	}
	d.logs, err = os.Create(filepath.Join(logDir, "dockerd.log"))
	if err != nil {
		return err
	}

	host := "unix://" + filepath.Join(d.root, "docker.sock")
	configPath := filepath.Join(d.root, "daemon.json")
	daemonConfig := map[string]any{
		"hosts":     []string{host},
		"data-root": d.dataRoot,
		"exec-root": d.execRoot,
		"pidfile":   filepath.Join(d.root, "docker.pid"),
		// Share image layers instead of copying every parent layer with VFS.
		"storage-driver":  "overlay2",
		"exec-opts":       []string{"native.cgroupdriver=cgroupfs"},
		"default-runtime": runtimeName,
		"experimental":    true,
	}
	if !inputs.Native {
		variants, err := RuntimeVariants("docker")
		if err != nil {
			return err
		}
		// Registration describes the declared runsc binary; --runtime selects
		// what containers use. Native mode needs no release or registration.
		daemonConfig["runtimes"] = runtimeDefinitions(runsc, "runsc", append([]string{
			// Keep the reusable gofer namespace under fixture ownership.
			"--shared-root=" + d.root,
			"--sidecar-usage-policy=STRICT",
			"--debug",
			"--debug-log=" + filepath.Join(logDir, "runsc.%TEST%.%TIMESTAMP%.%COMMAND%.log"),
		}, inputs.RuntimeArgs...), variants)
	}
	if inputs.IPv6 {
		// Match the bridge configuration required by the iptables/nftables
		// suites. Each owned daemon runs in its own test network namespace.
		daemonConfig["ipv6"] = true
		daemonConfig["fixed-cidr-v6"] = "2001:db8:1::/64"
	}
	var cgroupParent string
	if cgroup.IsOnlyV2() {
		// The fixture owns this shared parent for the entire suite. Individual
		// runtimes must not try to remove it while sibling containers use it.
		cgroupParent = "/" + filepath.Base(d.root)
		daemonConfig["cgroup-parent"] = cgroupParent
	}
	cfg, err := json.Marshal(daemonConfig)
	if err != nil {
		return err
	}
	if err := os.WriteFile(configPath, cfg, 0600); err != nil {
		return err
	}
	d.env = make(map[string]*string)
	environment := map[string]string{
		"DOCKER_HOST":        host,
		"DOCKER_TLS_VERIFY":  "",
		"DOCKER_CERT_PATH":   "",
		"DOCKER_CONTEXT":     "",
		"DOCKER_API_VERSION": "",
	}
	if !inputs.Native {
		environment["GVISOR_SIDECAR_BINARIES_DIR"] = sidecars
	}
	for key, value := range environment {
		if old, ok := os.LookupEnv(key); ok {
			d.env[key] = &old
		} else {
			d.env[key] = nil
		}
		if err := os.Setenv(key, value); err != nil {
			return err
		}
	}
	d.oldRuntime, d.oldConfig = *runtime, *config
	*runtime, *config = runtimeName, configPath
	d.configured = true

	d.cmd = exec.Command("dockerd", "--config-file", configPath)
	d.cmd.Stdout, d.cmd.Stderr = d.logs, d.logs
	// Give cleanup a separate process group to stop during startup or shutdown.
	d.cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if cgroupParent != "" {
		// The v2 root omits memory.swap.max, so Docker mistakes it for a
		// kernel without swap limits. Put the daemon in its own leaf, leaving
		// the owned parent empty so controllers can serve container siblings.
		// v1 exposes its controller files at the root and needs no relocation.
		d.cgroup, err = cgroup.NewFromPath(filepath.Join(cgroupParent, "daemon"), false)
		if err != nil {
			return fmt.Errorf("create Docker cgroup: %w", err)
		}
		if err := d.cgroup.Install(&specs.LinuxResources{}); err != nil {
			return fmt.Errorf("install Docker cgroup: %w", err)
		}
		fd, err := d.cgroup.CloneIntoCgroup()
		if err != nil {
			return fmt.Errorf("open Docker cgroup for child creation: %w", err)
		}
		defer fd.Close()
		d.cmd.SysProcAttr.UseCgroupFD = true
		d.cmd.SysProcAttr.CgroupFD = int(fd.Fd())
	}
	if err := d.cmd.Start(); err != nil {
		return fmt.Errorf("start dockerd: %w", err)
	}
	d.done = make(chan struct{})
	go func() {
		d.waitErr = d.cmd.Wait()
		close(d.done)
	}()
	cli, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
	if err != nil {
		return err
	}
	defer cli.Close()
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	tick := time.NewTicker(100 * time.Millisecond)
	defer tick.Stop()
	for {
		pingCtx, pingCancel := context.WithTimeout(ctx, time.Second)
		_, err := cli.Ping(pingCtx)
		pingCancel()
		if err == nil {
			break
		}
		select {
		case <-d.done:
			return fmt.Errorf("dockerd exited during startup: %v", d.waitErr)
		case <-ctx.Done():
			return fmt.Errorf("wait for dockerd: %w (last ping: %v)", ctx.Err(), err)
		case <-tick.C:
		}
	}
	info, err := cli.Info(ctx)
	if err != nil {
		return fmt.Errorf("get private daemon info: %w", err)
	}
	if info.DockerRootDir != d.dataRoot || info.DefaultRuntime != runtimeName {
		return fmt.Errorf("wrong daemon: data root=%q, default runtime=%q", info.DockerRootDir, info.DefaultRuntime)
	}
	log.Printf("Private Docker %s, runtime=%s, storage=%s, cgroup=%s/%s, root=%s", info.ServerVersion, info.DefaultRuntime, info.Driver, info.CgroupDriver, info.CgroupVersion, info.DockerRootDir)
	if !inputs.Native {
		log.Printf("Declared runsc=%s, strict sidecars=%s", runsc, sidecars)
	}
	var fs syscall.Statfs_t
	if err := syscall.Statfs(d.dataRoot, &fs); err != nil {
		return fmt.Errorf("inspect private image filesystem: %w", err)
	}
	log.Printf("Private image filesystem: capacity=%d bytes, available=%d bytes", fs.Blocks*uint64(fs.Bsize), fs.Bavail*uint64(fs.Bsize))
	for _, archive := range inputs.Images {
		if err := loadImageArchive(archive); err != nil {
			return err
		}
	}
	return nil
}

func loadImageArchive(archive string) error {
	// The existing Docker CLI handles HTTP and streamed image-load errors.
	// Only declared archives are loaded; the fixture does not pull images.
	// Image imports share the test action's timeout budget.
	output, err := exec.Command(dockerCLIPath(), "load", "--input", archive).CombinedOutput()
	if err != nil {
		return fmt.Errorf("load declared image archive %q: %w\n%s", archive, err, output)
	}
	log.Printf("Loaded declared image archive %s:\n%s", archive, output)
	return nil
}

// waitForOwnedIPv6Gateway waits for the private bridge's gateway to finish
// duplicate address detection. Container startup can return while the address
// is tentative, causing host connections to select an unrelated source address.
// Check per container: daemon API readiness does not imply bridge readiness.
func waitForOwnedIPv6Gateway(ctx context.Context, cli *client.Client, gateway string) error {
	if *dockerTestConfig == "" {
		return nil
	}
	data, err := os.ReadFile(*config)
	if err != nil {
		return fmt.Errorf("read private Docker configuration: %w", err)
	}
	var cfg struct {
		Hosts []string `json:"hosts"`
	}
	if err := json.Unmarshal(data, &cfg); err != nil {
		return fmt.Errorf("decode private Docker hosts: %w", err)
	}
	if len(cfg.Hosts) != 1 || !strings.HasPrefix(cfg.Hosts[0], "unix:///") {
		return fmt.Errorf("private Docker configuration has invalid hosts: %q", cfg.Hosts)
	}
	if cli.DaemonHost() != cfg.Hosts[0] {
		// An explicitly selected external daemon is not in our network namespace.
		return nil
	}
	ip := net.ParseIP(gateway)
	if ip == nil || ip.To4() != nil || ip.IsUnspecified() {
		return fmt.Errorf("invalid IPv6 gateway %q for private Docker daemon", gateway)
	}
	handle, err := netlink.NewHandle(unix.NETLINK_ROUTE)
	if err != nil {
		return fmt.Errorf("open route netlink socket: %w", err)
	}
	defer handle.Close()
	return testutil.PollContext(ctx, func() error {
		if err := ctx.Err(); err != nil {
			return err
		}
		// Retain netlink's normal socket limit, shortened to the caller's
		// remaining budget rather than a separate readiness timeout.
		timeout := netlink.GetSocketTimeout()
		if deadline, ok := ctx.Deadline(); ok {
			timeout = min(timeout, time.Until(deadline))
		}
		if timeout <= 0 {
			return context.DeadlineExceeded
		}
		if err := handle.SetSocketTimeout(timeout); err != nil {
			return backoff.Permanent(fmt.Errorf("set route netlink socket timeout: %w", err))
		}
		addresses, err := handle.AddrList(nil, netlink.FAMILY_V6)
		if errors.Is(err, netlink.ErrDumpInterrupted) {
			// Partial dumps cannot establish that the gateway is ready.
			return fmt.Errorf("list host IPv6 addresses: %w", err)
		}
		if err != nil {
			return backoff.Permanent(fmt.Errorf("list host IPv6 addresses: %w", err))
		}
		var address *netlink.Addr
		for i := range addresses {
			if addresses[i].IP.Equal(ip) {
				if address != nil {
					return backoff.Permanent(fmt.Errorf("IPv6 gateway %s has multiple local addresses", ip))
				}
				address = &addresses[i]
			}
		}
		if address == nil {
			return fmt.Errorf("IPv6 gateway %s has no local address", ip)
		}
		if address.Flags&unix.IFA_F_DADFAILED != 0 {
			return backoff.Permanent(fmt.Errorf("IPv6 gateway %s on interface %d failed duplicate address detection", ip, address.LinkIndex))
		}
		if address.Flags&unix.IFA_F_TENTATIVE != 0 {
			return fmt.Errorf("IPv6 gateway %s on interface %d is tentative", ip, address.LinkIndex)
		}
		return nil
	})
}

func (d *testDaemon) close(failed bool) error {
	var errs []error
	if d.done != nil {
		select {
		case <-d.done:
			errs = append(errs, fmt.Errorf("dockerd exited before cleanup: %v", d.waitErr))
		default:
			if err := d.cmd.Process.Signal(syscall.SIGTERM); err != nil && !errors.Is(err, os.ErrProcessDone) {
				errs = append(errs, fmt.Errorf("stop dockerd: %w", err))
			}
			select {
			case <-d.done:
			case <-time.After(15 * time.Second):
				errs = append(errs, errors.New("dockerd did not stop within 15 seconds"))
				if err := syscall.Kill(-d.cmd.Process.Pid, syscall.SIGKILL); err != nil && !errors.Is(err, syscall.ESRCH) {
					errs = append(errs, fmt.Errorf("kill dockerd process group: %w", err))
				}
				<-d.done
			}
			if d.waitErr != nil {
				errs = append(errs, fmt.Errorf("wait for dockerd: %w", d.waitErr))
			}
		}
		if err := syscall.Kill(-d.cmd.Process.Pid, syscall.SIGKILL); err != nil && !errors.Is(err, syscall.ESRCH) {
			errs = append(errs, fmt.Errorf("clean up dockerd process group: %w", err))
		}
	}
	if d.cgroup != nil {
		daemon := d.cgroup.MakePath("")
		if err := removeDockerChildCgroups(filepath.Dir(daemon), daemon); err != nil {
			errs = append(errs, fmt.Errorf("remove Docker child cgroups: %w", err))
		}
		if err := d.cgroup.Uninstall(); err != nil {
			errs = append(errs, fmt.Errorf("remove Docker cgroup: %w", err))
		}
	}
	if d.logs != nil {
		if err := d.logs.Close(); err != nil {
			errs = append(errs, fmt.Errorf("close dockerd log: %w", err))
		}
		if failed || len(errs) != 0 {
			if output, err := os.ReadFile(d.logs.Name()); err != nil {
				errs = append(errs, fmt.Errorf("read dockerd log: %w", err))
			} else {
				log.Printf("dockerd output:\n%s", output)
			}
		}
	}
	// The daemon is stopped and reaped before unmounting or removing owned state.
	if d.root != "" {
		specutils.UnmountNullNetNS(d.root)
		// Docker retains the default network namespace bind mount after the
		// last host-network container exits and the daemon shuts down.
		if err := syscall.Unmount(filepath.Join(d.execRoot, "netns", "default"), 0); err != nil && !errors.Is(err, syscall.ENOENT) && !errors.Is(err, syscall.EINVAL) {
			errs = append(errs, fmt.Errorf("unmount private Docker default network namespace: %w", err))
		}
		// Docker can retain its data-root bind mount when its path differs
		// from the filesystem-relative root beneath a parent bind mount.
		if err := syscall.Unmount(d.dataRoot, 0); err != nil && !errors.Is(err, syscall.ENOENT) && !errors.Is(err, syscall.EINVAL) {
			errs = append(errs, fmt.Errorf("unmount private Docker data root: %w", err))
		}
		if err := os.RemoveAll(d.root); err != nil {
			errs = append(errs, fmt.Errorf("remove private Docker state: %w", err))
		}
	}
	if d.configured {
		*runtime, *config = d.oldRuntime, d.oldConfig
	}
	for key, value := range d.env {
		var err error
		if value == nil {
			err = os.Unsetenv(key)
		} else {
			err = os.Setenv(key, *value)
		}
		if err != nil {
			errs = append(errs, fmt.Errorf("restore environment %q: %w", key, err))
		}
	}
	return errors.Join(errs...)
}

// removeDockerChildCgroups removes empty descendants left by Docker's builders.
// The daemon leaf stays with Uninstall so its existing removal retry applies.
// rmdir rejects populated cgroups; no tasks or control files are removed.
func removeDockerChildCgroups(path, daemon string) error {
	entries, err := os.ReadDir(path)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	var errs []error
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		child := filepath.Join(path, entry.Name())
		if err := removeDockerChildCgroups(child, daemon); err != nil {
			errs = append(errs, err)
			continue
		}
		if child != daemon {
			if err := syscall.Rmdir(child); err != nil && !os.IsNotExist(err) {
				errs = append(errs, fmt.Errorf("remove %q: %w", child, err))
			}
		}
	}
	return errors.Join(errs...)
}
