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

// Binary observer records the directory supplied by the worker image and
// checks that common system programs are absent.
package main

import (
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"runtime"
	"syscall"
)

func observe(out string) error {
	const marker = "/gvisor-worker-root"
	fi, err := os.Lstat(marker)
	if err != nil {
		return err
	}
	if !fi.IsDir() {
		return fmt.Errorf("%s is not a directory", marker)
	}
	entries, err := os.ReadDir(marker)
	if err != nil {
		return err
	}
	if len(entries) != 0 {
		return fmt.Errorf("%s has %d entries", marker, len(entries))
	}
	paths := []string{"/bin/sh", "/bin/bash", "/bin/busybox", "/usr/bin/env", "/usr/bin/cc", "/usr/bin/gcc", "/usr/bin/clang"}
	for _, path := range paths {
		if _, err := os.Lstat(path); !errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("%s: expected ENOENT, got %v", path, err)
		}
	}
	var uts syscall.Utsname
	if err := syscall.Uname(&uts); err != nil {
		return err
	}
	var machine []byte
	for _, c := range uts.Machine {
		if c == 0 {
			break
		}
		machine = append(machine, byte(c))
	}
	f, err := os.Open("/proc/self/exe")
	if err != nil {
		return err
	}
	defer f.Close()
	h := sha256.New()
	n, err := io.Copy(h, f)
	if err != nil {
		return err
	}
	b, err := json.Marshal(struct {
		Arch, Machine, Directory, ExecutableSHA256 string
		ExecutableBytes                            int64
		Absent                                     []string
	}{runtime.GOARCH, string(machine), marker, fmt.Sprintf("%x", h.Sum(nil)), n, paths})
	if err != nil {
		return err
	}
	return os.WriteFile(out, b, 0644)
}

func main() {
	if len(os.Args) != 2 {
		log.Fatal("usage: observer output.json")
	}
	if err := observe(os.Args[1]); err != nil {
		log.Fatal(err)
	}
}
