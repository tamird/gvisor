// Copyright 2024 The gVisor Authors.
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

// Package integration provides end-to-end integration tests for runsc.
package integration

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/docker/docker/api/types/mount"
	"gvisor.dev/gvisor/pkg/test/dockerutil"
	"gvisor.dev/gvisor/pkg/test/testutil"
)

// testVariant is a variant of the gVisor in Docker test.
type testVariant struct {
	Name          string
	User          string
	WorkDir       string
	CapAdd        []string
	Args          []string
	MountCgroupfs bool
}

// run runs the test variant.
func (test testVariant) run(ctx context.Context, logger testutil.Logger) (string, error) {
	var expected struct {
		Directory string `json:"directory"`
		Files     map[string]struct {
			Bytes  int    `json:"bytes"`
			SHA256 string `json:"sha256"`
		} `json:"files"`
	}
	identityFile, diagnostic := os.LookupEnv("GVISOR_DIAGNOSTIC_RUNTIME_IDENTITIES")
	var runscPath, gvisorBinDir string
	if diagnostic {
		if !filepath.IsAbs(identityFile) {
			return "", fmt.Errorf("diagnostic runtime identities require an absolute path")
		}
		data, err := os.ReadFile(identityFile)
		if err != nil {
			return "", err
		}
		if err := json.Unmarshal(data, &expected); err != nil {
			return "", err
		}
		if !filepath.IsAbs(expected.Directory) {
			return "", fmt.Errorf("diagnostic runtime directory requires an absolute path")
		}
		// Keep the runtime bytes fixed across qualification-only test edits.
		runscPath = filepath.Join(expected.Directory, "runsc")
		gvisorBinDir = filepath.Join(expected.Directory, "gvisor-bin")
	} else {
		var err error
		runscPath, err = testutil.FindRunsc()
		if err != nil {
			return "", fmt.Errorf("cannot locate runsc binary: %v", err)
		}
		gvisorBinDir, err = testutil.FindFile("release/gvisor-bin")
		if err != nil {
			return "", fmt.Errorf("cannot locate gvisor-bin directory: %v", err)
		}
	}
	runscRealPath, err := filepath.EvalSymlinks(runscPath)
	if err != nil {
		return "", fmt.Errorf("cannot resolve runsc path %q: %v", runscPath, err)
	}
	gvisorBinEntries, err := os.ReadDir(gvisorBinDir)
	if err != nil {
		return "", fmt.Errorf("cannot read gvisor-bin directory %q: %v", gvisorBinDir, err)
	}
	var sidecarMounts []mount.Mount
	for _, entry := range gvisorBinEntries {
		src := filepath.Join(gvisorBinDir, entry.Name())
		realPath, err := filepath.EvalSymlinks(src)
		if err != nil {
			return "", fmt.Errorf("cannot resolve sidecar path %q: %v", src, err)
		}
		sidecarMounts = append(sidecarMounts, mount.Mount{
			Type:     mount.TypeBind,
			Source:   realPath,
			Target:   "/gvisor-bin/" + entry.Name(),
			ReadOnly: true,
		})
	}
	if diagnostic {
		files := map[string]string{"runsc": runscRealPath}
		for _, m := range sidecarMounts {
			files[filepath.Base(m.Target)] = m.Source
		}
		if len(expected.Files) != len(files) {
			return "", fmt.Errorf("runtime identity count: got %d files, want %d", len(files), len(expected.Files))
		}
		for name, path := range files {
			data, err := os.ReadFile(path)
			if err != nil {
				return "", err
			}
			want, ok := expected.Files[name]
			digest := fmt.Sprintf("%x", sha256.Sum256(data))
			if !ok || len(data) != want.Bytes || digest != want.SHA256 {
				return "", fmt.Errorf("runtime identity mismatch: %s (%d bytes, %s)", path, len(data), digest)
			}
			logger.Logf("Diagnostic mounted runtime: %s, %d bytes, %s", path, len(data), digest)
		}
	}
	d := dockerutil.MakeNativeContainer(ctx, logger)
	defer d.CleanUp(ctx)
	opts := dockerutil.RunOpts{
		Image:   "basic/integrationtest",
		User:    test.User,
		WorkDir: test.WorkDir,
		SecurityOpts: []string{
			// Disable default seccomp filter which blocks `mount(2)` and others.
			"seccomp=unconfined",

			// Disable AppArmor which also blocks mounts.
			"apparmor=unconfined",

			// Set correct SELinux label; this allows ptrace.
			"label=type:container_engine_t",
		},
		CapAdd: test.CapAdd,
		Mounts: append([]mount.Mount{
			// Mount the runtime binary.
			{
				Type:     mount.TypeBind,
				Source:   runscRealPath,
				Target:   "/runtime",
				ReadOnly: true,
			},
		}, sidecarMounts...),
	}
	if test.MountCgroupfs {
		// Match the host cgroupfs mount so /proc/self/cgroup paths resolve
		// within the same cgroup namespace.
		opts.CgroupnsMode = "host"
		opts.Mounts = append(opts.Mounts, mount.Mount{
			Type:     mount.TypeBind,
			Source:   "/sys/fs/cgroup",
			Target:   "/sys/fs/cgroup",
			ReadOnly: false,
		})
	}
	const wantMessage = "It became a jumble of words, a litany, almost a kind of glossolalia."
	args := []string{
		"/runtime",
		"--debug=true",
		"--debug-log=/dev/stderr",
	}
	args = append(args, test.Args...)
	args = append(args, "do")
	// Split the message into separate substrings to avoid matching on the message
	// just because the sandbox argv in the debug logs matches it
	// (we want to match stdout specifically).
	splitWantMessage := strings.Split(wantMessage, " ")
	var command []string
	for _, arg := range splitWantMessage {
		command = append(command, fmt.Sprintf("printf '%%s ' %q", arg))
	}
	args = append(args, "/bin/sh", "-c", strings.Join(command, " && "))
	// Qualification-only instrumentation leaves the runtime and its sidecars at
	// their original paths. The untraced runs use the original command unchanged.
	tool, tracing := os.LookupEnv("GVISOR_DIAGNOSTIC_STRACE")
	if tracing {
		outputRoot, ok := os.LookupEnv("TEST_UNDECLARED_OUTPUTS_DIR")
		if !ok || !filepath.IsAbs(outputRoot) || !filepath.IsAbs(tool) {
			return "", fmt.Errorf("diagnostic tracing requires absolute tool and Bazel output paths")
		}
		output, err := os.MkdirTemp(outputRoot, "strace-")
		if err != nil {
			return "", fmt.Errorf("creating trace output: %w", err)
		}
		opts.Mounts = append(opts.Mounts,
			mount.Mount{Type: mount.TypeBind, Source: tool, Target: "/diagnostic-tool", ReadOnly: true},
			mount.Mount{Type: mount.TypeBind, Source: output, Target: "/diagnostic-output"},
		)
		args = append([]string{
			"/diagnostic-tool/loader", "--library-path", "/diagnostic-tool/lib",
			"/diagnostic-tool/strace", "-ff", "-yy", "-ttt", "-s", "256",
			"-e", "trace=%process,%file,%desc,%creds,setns,unshare,mount,umount2,prctl",
			"-o", "/diagnostic-output/trace",
		}, args...)
	}
	logger.Logf("Running: %v", args)
	got, err := d.Run(ctx, opts, args...)
	if tracing {
		state, err := d.Status(ctx)
		logger.Logf("Diagnostic tracer container status: %+v; inspect error: %v", state, err)
	}
	got = strings.TrimSpace(got)
	if err != nil {
		return got, err
	}
	if !strings.Contains(got, wantMessage) {
		return got, fmt.Errorf("did not observe substring %q in logs", wantMessage)
	}
	return got, nil
}

// failureCases returns modified versions of this same test that are expected
// to fail. Verifying that these variants fail ensures that each test variant
// runs with the minimal amount of deviations from the default configuration.
func (test testVariant) failureCases() []testVariant {
	failureCase := func(name string) testVariant {
		copy := test
		copy.Name = name
		return copy
	}
	var failureCases []testVariant
	if test.MountCgroupfs {
		copy := failureCase("without cgroupfs mounted")
		copy.MountCgroupfs = false
		failureCases = append(failureCases, copy)
	}
	for i, capAdd := range test.CapAdd {
		copy := failureCase(fmt.Sprintf("without capability %s", capAdd))
		copy.CapAdd = append(append([]string(nil), test.CapAdd[:i]...), test.CapAdd[i+1:]...)
		failureCases = append(failureCases, copy)
	}
	for _, tryRemoveArg := range []string{
		"--rootless=true",
		"--ignore-cgroups=true",
	} {
		if index := slices.Index(test.Args, tryRemoveArg); index != -1 {
			copy := failureCase(fmt.Sprintf("without argument %s", tryRemoveArg))
			copy.Args = append(append([]string(nil), test.Args[:index]...), test.Args[index+1:]...)
			failureCases = append(failureCases, copy)
		}
	}
	return failureCases
}

// TestGVisorInDocker runs `runsc` inside a non-gVisor container.
// This is used in contexts such as Dangerzone:
// https://gvisor.dev/blog/2024/09/23/safe-ride-into-the-dangerzone/
func TestGVisorInDocker(t *testing.T) {
	ctx := context.Background()
	for _, test := range []testVariant{
		{
			Name: "Rootful",
			User: "root",
			CapAdd: []string{
				// Necessary to set up networking (creating veth devices).
				"NET_ADMIN",
				// Necessary to set up networking, which calls `ip netns add` which
				// calls `mount(2)`.
				"SYS_ADMIN",
			},
			// Mount cgroupfs as writable, otherwise the runtime won't be able to
			// set up cgroups.
			MountCgroupfs: true,
		},
		{
			Name: "Rootful without networking",
			User: "root",
			CapAdd: []string{
				// "Can't run sandbox process in minimal chroot since we don't have CAP_SYS_ADMIN"
				"SYS_ADMIN",
			},
			Args: []string{
				"--network=none",
			},
			MountCgroupfs: true,
		},
		{
			Name: "Rootful with host networking",
			User: "root",
			CapAdd: []string{
				// Necessary to set up networking (creating veth devices).
				"NET_ADMIN",
				// Necessary to set up networking, which calls `ip netns add` which
				// calls `mount(2)`.
				"SYS_ADMIN",
			},
			Args: []string{
				"--network=host",
			},
			MountCgroupfs: true,
		},
		{
			Name: "Rootful without networking and cgroupfs",
			User: "root",
			CapAdd: []string{
				// "Can't run sandbox process in minimal chroot since we don't have CAP_SYS_ADMIN"
				"SYS_ADMIN",
			},
			Args: []string{
				"--network=none",
				"--ignore-cgroups=true",
			},
		},
		{
			Name:    "Rootless",
			User:    "nonroot",
			WorkDir: "/home/nonroot",
			Args: []string{
				"--rootless=true",
			},
		},
		{
			Name:    "Rootless without networking",
			User:    "nonroot",
			WorkDir: "/home/nonroot",
			Args: []string{
				"--rootless=true",
				"--network=none",
			},
		},
	} {
		t.Run(test.Name, func(t *testing.T) {
			if logs, err := test.run(ctx, t); err != nil {
				t.Fatalf("Error: %v; logs:\n%s", err, logs)
			}
			for _, failureCase := range test.failureCases() {
				t.Run(failureCase.Name, func(t *testing.T) {
					if logs, err := failureCase.run(ctx, t); err == nil {
						t.Fatalf("Failure case unexpectedly succeeded; logs:\n%s", logs)
					}
				})
			}
		})
	}
}
