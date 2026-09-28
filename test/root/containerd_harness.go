// Copyright 2026 The gVisor Authors.
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

package root

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/docker/docker/api/types/mount"
	"gvisor.dev/gvisor/pkg/test/dockerutil"
	"gvisor.dev/gvisor/pkg/test/testutil"
	"gvisor.dev/gvisor/runsc/flag"
)

var (
	containerdVersion = flag.String("containerd_version", "2.2.3",
		"containerd version to test against; must be baked into the harness image")
	harnessImage = flag.String("harness_image", "containerd/harness",
		"harness image, relative to images/")
	harnessTestImages = flag.String("harness_test_images",
		"basic/alpine,basic/python,basic/busybox,basic/symlink-resolv,basic/httpd,basic/ubuntu",
		"images to stage into the harness; must match the load-* deps of containerd-test-% in the Makefile")
	noHarness = flag.Bool("no_harness", false,
		"run directly against a host containerd instead of the harness (legacy path)")
)

const (
	harnessEnv = "GVISOR_CONTAINERD_HARNESS"
	runtimeDir = "/runtime"
	imageDir   = "/cri-images"
)

func inHarness() bool {
	return os.Getenv(harnessEnv) != ""
}

func useHarness() bool {
	return !*noHarness && !inHarness()
}

// runInHarness runs the test in a containerd harness.
// Returns a nonzero status on setup, execution or cleanup failure.
func runInHarness(ctx context.Context) int {
	logger := testutil.DefaultLogger("harness")

	code, err := launch(ctx, logger)
	if err != nil {
		fmt.Fprintf(os.Stderr, "harness: %v\n", err)
		if code == 0 {
			code = 1
		}
	}
	return code
}

// launch launches the containerd harness.
// Returns its status and any setup, execution or cleanup error.
func launch(ctx context.Context, logger testutil.Logger) (code int, retErr error) {
	self, err := os.Executable()
	if err != nil {
		return 1, fmt.Errorf("cannot locate test binary: %w", err)
	}
	if self, err = filepath.EvalSymlinks(self); err != nil {
		return 1, fmt.Errorf("cannot resolve test binary: %w", err)
	}

	// All staged inputs belong to this invocation, including partial setup.
	dir, err := os.MkdirTemp("", "containerd-harness-")
	if err != nil {
		return 1, err
	}
	defer func() { retErr = errors.Join(retErr, os.RemoveAll(dir)) }()

	runtime := dockerutil.Runtime()
	if runtime == "" {
		runtime = "runsc"
	}

	// Stage the runtime and test binary into the containerd harness.
	runtimeHostDir := filepath.Join(dir, "runtime")
	if err := stageRuntime(runtimeHostDir, self); err != nil {
		return 1, err
	}

	var mounts []mount.Mount
	mounts = append(mounts, mount.Mount{
		Type:     mount.TypeBind,
		Source:   runtimeHostDir,
		Target:   runtimeDir,
		ReadOnly: true,
	})

	// Translate the selected runtime's path for the test inside the harness.
	daemonJSON := filepath.Join(dir, "daemon.json")
	if err := writeDaemonConfig(daemonJSON, runtime); err != nil {
		return 1, err
	}
	mounts = append(mounts, mount.Mount{
		Type:     mount.TypeBind,
		Source:   daemonJSON,
		Target:   "/etc/docker/daemon.json",
		ReadOnly: true,
	})

	// Stage the test images.
	staged := filepath.Join(dir, "images")
	if err := stageImages(staged, logger); err != nil {
		return 1, err
	}
	mounts = append(mounts,
		mount.Mount{
			Type:     mount.TypeBind,
			Source:   staged,
			Target:   imageDir,
			ReadOnly: true,
		},
		mount.Mount{
			Type:   mount.TypeBind,
			Source: "/sys/fs/cgroup",
			Target: "/sys/fs/cgroup",
		},
		mount.Mount{Type: mount.TypeTmpfs, Target: "/tmp"},
		mount.Mount{Type: mount.TypeVolume, Target: "/var/lib"},
	)

	d := dockerutil.MakeNativeContainer(ctx, logger)
	defer func() { retErr = errors.Join(retErr, d.CleanUp(ctx)) }()

	opts := dockerutil.RunOpts{
		Image:        *harnessImage,
		Privileged:   true,
		Init:         true,
		CgroupnsMode: "host",
		SecurityOpts: []string{
			"seccomp=unconfined",
			"apparmor=unconfined",
			"label=type:container_engine_t",
		},
		Mounts: mounts,
		Env: []string{
			harnessEnv + "=1",
			"CONTAINERD_VERSION=" + *containerdVersion,
			"RUNTIME=" + runtime,
			"GVISOR_CRI_IMAGE_DIR=" + imageDir,
			"GVISOR_SIDECAR_BINARIES_DIR=" + runtimeDir + "/gvisor-bin",
			"TEST_TMPDIR=",
		},
	}

	// Forward effective flags, replacing paths and runtime selection that the
	// outer invocation may have supplied separately or through its environment.
	binName := filepath.Base(self)
	args := []string{filepath.Join(runtimeDir, binName)}
	flag.CommandLine.Visit(func(f *flag.Flag) {
		if f.Name != "runtime" && f.Name != "config_path" {
			args = append(args, "--"+f.Name+"="+f.Value.String())
		}
	})
	args = append(args, "--runtime="+runtime, "--config_path=/etc/docker/daemon.json")
	if positional := flag.CommandLine.Args(); len(positional) != 0 {
		args = append(args, "--")
		args = append(args, positional...)
	}
	logger.Logf("launching harness: containerd %s, runtime %s", *containerdVersion, runtime)

	if err := d.Create(ctx, opts, args...); err != nil {
		return 1, fmt.Errorf("creating harness container: %w", err)
	}
	if err := d.Start(ctx); err != nil {
		return 1, fmt.Errorf("starting harness container: %w", err)
	}

	streamCtx, cancelStream := context.WithCancel(ctx)
	defer cancelStream()
	streamDone := make(chan error, 1)
	go func() {
		streamDone <- d.StreamOutput(streamCtx, os.Stdout, os.Stderr)
	}()

	waitErr := d.Wait(ctx)
	var inspectErr error
	if waitErr != nil {
		// Preserve complete logs for an exited test, including a failed one.
		// If it may still be running, stop following so cleanup can run.
		state, err := d.Status(ctx)
		inspectErr = err
		if err != nil || state.Running {
			cancelStream()
		}
	}
	if err := errors.Join(waitErr, inspectErr, <-streamDone); err != nil {
		return 1, fmt.Errorf("running harness container: %w", err)
	}
	return 0, nil
}

func stageRuntime(dir, self string) error {
	// Copy the release tree into the temporary directory.
	root, err := testutil.FindFile("release")
	if err != nil {
		return fmt.Errorf("cannot locate release tree: %w", err)
	}

	count := 0
	// Walk the release tree and copy it to the temporary directory so we can mount it into the
	// harness.
	err = filepath.Walk(root, func(p string, fi os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(root, p)
		if err != nil {
			return err
		}
		target := filepath.Join(dir, rel)
		if fi.IsDir() {
			return os.MkdirAll(target, 0755)
		}
		real, err := filepath.EvalSymlinks(p)
		if err != nil {
			return fmt.Errorf("cannot resolve %q: %w", p, err)
		}
		count++
		return copyFile(real, target, 0755)
	})
	if err != nil {
		return fmt.Errorf("copying release files: %w", err)
	}
	if count == 0 {
		return fmt.Errorf("release tree %q is empty", root)
	}

	// The caller already resolved the test binary's symlinks.
	binName := filepath.Base(self)
	if err := copyFile(self, filepath.Join(dir, binName), 0755); err != nil {
		return fmt.Errorf("copying test binary: %w", err)
	}

	return nil
}

func copyFile(src, dst string, mode os.FileMode) (retErr error) {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()

	if err := os.MkdirAll(filepath.Dir(dst), 0755); err != nil {
		return err
	}

	out, err := os.OpenFile(dst, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, mode)
	if err != nil {
		return err
	}
	defer func() { retErr = errors.Join(retErr, out.Close()) }()

	if _, err := io.Copy(out, in); err != nil {
		return err
	}
	return out.Sync()
}

// writeDaemonConfig gives the inner test the selected runtime's staged path.
func writeDaemonConfig(p, runtime string) error {
	body := fmt.Sprintf(`{"runtimes": {%q: {"path": %q}}}`,
		runtime, filepath.Join(runtimeDir, "runsc"))
	return os.WriteFile(p, []byte(body), 0644)
}

// stageImages stages the images listed in harnessTestImages into the harness.
// Each invocation exports the images currently loaded in its selected daemon.
func stageImages(dir string, logger testutil.Logger) error {
	if err := os.MkdirAll(dir, 0755); err != nil {
		return err
	}
	for _, image := range strings.Split(*harnessTestImages, ",") {
		image = strings.TrimSpace(image)
		if image == "" {
			continue
		}
		p := filepath.Join(dir, tarNameForImage(image))
		f, err := os.OpenFile(p, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0644)
		if err != nil {
			return err
		}
		saveErr := dockerutil.Save(logger, image, f)
		closeErr := f.Close()
		if err := errors.Join(saveErr, closeErr); err != nil {
			return fmt.Errorf("staging %q (is it loaded? try `make load-%s`): %w",
				image, strings.ReplaceAll(image, "/", "_"), err)
		}
	}
	return nil
}

// tarNameForImage returns the name of the tar file for the given image.
func tarNameForImage(image string) string {
	return strings.ReplaceAll(image, "/", "_") + ".tar"
}
