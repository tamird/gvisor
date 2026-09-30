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

// stage gives the pinned Syzkaller Makefile and smoke script writable working
// directories while retaining the declared compiler's execroot-relative paths.
package main

import (
	"bytes"
	"debug/elf"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

type config struct {
	Sources     []string `json:"sources"`
	SourceRoot  string   `json:"source_root"`
	Go          string   `json:"go"`
	GoRoot      string   `json:"goroot"`
	Make        string   `json:"make"`
	Proxy       string   `json:"proxy"`
	CC          string   `json:"cc"`
	CXX         string   `json:"cxx"`
	Linker      string   `json:"linker"`
	ResourceDir string   `json:"resource_dir"`
	Sysroot     string   `json:"sysroot"`
	Tar         string   `json:"tar"`
	TarEnv      []string `json:"tar_env"`
	ToolRoots   []string `json:"tool_roots"`
}

var (
	configPath = flag.String("config", "", "declared build configuration")
	toolRoot   = flag.String("tools", ".", "root of the declared compiler paths")
	source     = flag.String("source", "", "built Syzkaller tree for the smoke test")
	runtimeTar = flag.String("runtime", "", "canonical gVisor release archive for the smoke test")
	output     = flag.String("output", "", "Syzkaller build output directory")
)

var products = []string{
	"bin/syz-manager",
	"bin/linux_amd64/syz-execprog",
	"bin/linux_amd64/syz-executor",
	"tools/gvisor-smoke-test.sh",
}

func main() {
	flag.Parse()
	if err := run(); err != nil {
		log.Fatal(err)
	}
}

func run() error {
	data, err := os.ReadFile(*configPath)
	if err != nil {
		return err
	}
	var cfg config
	if err := json.Unmarshal(data, &cfg); err != nil {
		return err
	}
	root, err := filepath.Abs(*toolRoot)
	if err != nil {
		return err
	}
	work, err := os.MkdirTemp(os.Getenv("TEST_TMPDIR"), "syzkaller-stage-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(work)
	// Syzkaller splits SYZ_CC/SYZ_CXX with strings.Fields, not shell quoting.
	// https://github.com/google/syzkaller/blob/7808aef4a/sys/targets/targets.go#L736-L743
	if fields := strings.Fields(work); len(fields) != 1 || fields[0] != work {
		return fmt.Errorf("syzkaller requires a temporary directory without whitespace: %q", work)
	}
	stage := filepath.Join(work, "source")
	if err := os.Mkdir(stage, 0755); err != nil {
		return err
	}
	if *runtimeTar == "" {
		for _, path := range cfg.Sources {
			rel, err := filepath.Rel(cfg.SourceRoot, path)
			if err != nil || !filepath.IsLocal(rel) {
				return fmt.Errorf("source %q is outside %q", path, cfg.SourceRoot)
			}
			if err := copyFile(path, filepath.Join(stage, rel)); err != nil {
				return err
			}
		}
	} else {
		for _, path := range products {
			if err := copyFile(filepath.Join(*source, path), filepath.Join(stage, path)); err != nil {
				return err
			}
		}
	}
	// These names must not collide with upstream sources. The rule rejects
	// compiler inputs outside these roots, and Symlink rejects existing paths.
	for _, name := range cfg.ToolRoots {
		if err := os.Symlink(filepath.Join(root, name), filepath.Join(stage, name)); err != nil {
			return err
		}
	}
	sysroot := filepath.Join(work, "sysroot")
	if err := os.Mkdir(sysroot, 0755); err != nil {
		return err
	}
	if err := command(stage, cfg.TarEnv, filepath.Join(root, cfg.Tar),
		"-xf", filepath.Join(root, cfg.Sysroot), "-C", sysroot, "--no-same-owner"); err != nil {
		return err
	}
	// A complete GNU sysroot supplies the executor's static libraries. Select
	// its GCC installation explicitly, ignoring ambient driver configuration.
	// Leave language standards and static-link probes to upstream Make.
	// https://clang.llvm.org/docs/ClangCommandLineReference.html#cmdoption-clang-gcc-install-dir
	flags := []string{
		"--no-default-config",
		"--target=x86_64-linux-gnu",
		"--sysroot=" + sysroot,
		"--gcc-install-dir=" + filepath.Join(sysroot, "usr/lib/gcc/x86_64-linux-gnu/11"),
		"-resource-dir=" + filepath.Join(root, cfg.ResourceDir),
		"--ld-path=" + filepath.Join(root, cfg.Linker),
		"--rtlib=libgcc", "--unwindlib=libgcc", "--stdlib=libstdc++",
	}
	for name, compiler := range map[string]string{"cc": cfg.CC, "cxx": cfg.CXX} {
		// Make's executor input is source-relative. The smoke-mode probes read
		// stdin and write /dev/null; this is not an arbitrary-CWD compiler API.
		// https://github.com/google/syzkaller/blob/7808aef4a/sys/targets/targets.go#L1004-L1044
		var script strings.Builder
		fmt.Fprintf(&script, "#!/bin/sh\nset -eu\ncd %s\nexec", quote(stage))
		for _, arg := range append([]string{compiler}, flags...) {
			fmt.Fprintf(&script, " %s", quote(arg))
		}
		script.WriteString(" \"$@\"\n")
		if err := os.WriteFile(filepath.Join(work, name), []byte(script.String()), 0755); err != nil {
			return err
		}
	}
	compilerEnv := []string{
		"SYZ_CC_linux_amd64=" + filepath.Join(work, "cc"),
		"SYZ_CXX_linux_amd64=" + filepath.Join(work, "cxx"),
	}
	if *runtimeTar != "" {
		release, err := filepath.Abs(*runtimeTar)
		if err != nil {
			return err
		}
		// The smoke script passes this environment through sudo -E and
		// owns its runtime mounts and cleanup until it exits.
		return command(stage, append(os.Environ(), append(compilerEnv, "GVISOR_TARBALL_PATH="+release)...),
			"/bin/bash", "tools/gvisor-smoke-test.sh")
	}
	// A private Go cache plus a file-only module proxy keeps Make's Go commands
	// offline. Resolution/checksum verification belongs to module_proxy; no
	// user Go configuration, workspace or auto-downloaded SDK participates.
	// https://go.dev/ref/mod#module-proxy
	// https://go.dev/doc/toolchain#GOTOOLCHAIN
	proxy := (&url.URL{Scheme: "file", Path: filepath.Join(root, cfg.Proxy)}).String()
	env := append(compilerEnv,
		// Upstream Make invokes bare go, including while reading the Makefile.
		// Shell utilities retain the build worker's ordinary system locations.
		"PATH="+filepath.Join(root, filepath.Dir(cfg.Go))+":/usr/bin:/bin",
		// The declared Make toolchain exports its own executable for recursive
		// calls; it must stay absolute after Make changes working directory.
		// https://www.gnu.org/software/make/manual/html_node/MAKE-Variable.html
		"MAKE="+filepath.Join(root, cfg.Make),
		"GOROOT="+filepath.Join(root, cfg.GoRoot),
		"GOPROXY="+proxy, "GOSUMDB=off", "GOENV=off", "GOWORK=off", "GOTOOLCHAIN=local",
		"GOCACHE="+filepath.Join(work, "cache"),
		"GOMODCACHE="+filepath.Join(work, "modules"),
		"GOPATH="+filepath.Join(work, "gopath"),
		// Match the action's four-CPU allowance, including code generators.
		// https://pkg.go.dev/runtime#hdr-Environment_Variables
		"GOMAXPROCS=4",
	)
	// Keep upstream's revision flags, including during recursive Make. Explicit
	// revision/date and terminal colors avoid Git/tput probes in an archive.
	// https://github.com/google/syzkaller/blob/7808aef4a/Makefile#L44-L94
	// -trimpath prevents the random staging directory from entering binaries.
	// https://pkg.go.dev/cmd/go#hdr-Compile_packages_and_dependencies
	metadata, err := os.ReadFile(filepath.Join(stage, "gvisor-source.json"))
	if err != nil {
		return err
	}
	var version struct{ Revision, Date string }
	if err := json.Unmarshal(metadata, &version); err != nil {
		return err
	}
	if err := command(stage, env, filepath.Join(root, cfg.Make),
		"manager", "execprog", "executor", "NCORES=4", "RED=", "RESET=",
		"REV="+version.Revision, "GITREVDATE="+version.Date,
		"GOHOSTFLAGS=$(GOFLAGS) -mod=readonly -buildvcs=false -trimpath -p=4",
		"GOTARGETFLAGS=$(GOFLAGS) -mod=readonly -buildvcs=false -trimpath -p=4"); err != nil {
		return err
	}
	for _, name := range []string{"go.mod", "go.sum"} {
		original, err := os.ReadFile(filepath.Join(cfg.SourceRoot, name))
		if err != nil {
			return err
		}
		staged, err := os.ReadFile(filepath.Join(stage, name))
		if err != nil {
			return err
		}
		if !bytes.Equal(original, staged) {
			return fmt.Errorf("upstream Make changed %s; update the pinned module metadata", name)
		}
	}
	// Upstream permits dropping unsupported static-link flags. Its gVisor
	// guest contains no loader or shared libraries, so such an executor cannot
	// run there. Keep static PIE valid and reject only runtime dependencies.
	// https://github.com/google/syzkaller/blob/7808aef4a/vm/gvisor/gvisor.go#L100-L139
	executor, err := elf.Open(filepath.Join(stage, "bin/linux_amd64/syz-executor"))
	if err != nil {
		return fmt.Errorf("reading built syz-executor ELF: %w", err)
	}
	defer executor.Close()
	for _, prog := range executor.Progs {
		if prog.Type == elf.PT_INTERP {
			return fmt.Errorf("built syz-executor requires a dynamic loader absent from the gVisor smoke guest")
		}
	}
	libraries, err := executor.ImportedLibraries()
	if err != nil {
		return fmt.Errorf("reading built syz-executor libraries: %w", err)
	}
	if len(libraries) != 0 {
		return fmt.Errorf("built syz-executor requires shared libraries absent from the gVisor smoke guest: %v", libraries)
	}
	// Upstream can finish successfully without an executor when its compiler
	// probes fail. Requiring every product prevents publishing a partial tree.
	for _, path := range products {
		if err := copyFile(filepath.Join(stage, path), filepath.Join(*output, path)); err != nil {
			return err
		}
	}
	return nil
}

func command(dir string, env []string, name string, args ...string) error {
	cmd := exec.Command(name, args...)
	cmd.Dir, cmd.Env = dir, env
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("running %s %q in %s: %w", name, args, dir, err)
	}
	return nil
}

func quote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "'\\''") + "'"
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	info, err := in.Stat()
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() {
		return fmt.Errorf("expected regular file: %s", src)
	}
	if err := os.MkdirAll(filepath.Dir(dst), 0755); err != nil {
		return err
	}
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_EXCL|os.O_WRONLY, info.Mode().Perm())
	if err != nil {
		return err
	}
	_, err = io.Copy(out, in)
	closeErr := out.Close()
	if err != nil {
		return err
	}
	return closeErr
}
