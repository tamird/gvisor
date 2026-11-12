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

// Programs for preparing the unsigned release. No private key is available to
// the packaging actions.
package main

import (
	"crypto/sha512"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

// archive copies a release file and writes its GNU sha512sum checkfile.
func archive(src, dst, check string) error {
	f, err := os.Open(src)
	if err != nil {
		return err
	}
	defer f.Close()
	// A local Bazel sandbox may expose the declared input through a symlink.
	info, err := f.Stat()
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() {
		return fmt.Errorf("not a regular release file: %s", src)
	}
	out, err := os.Create(dst)
	if err != nil {
		return err
	}
	h := sha512.New()
	if _, err := io.Copy(io.MultiWriter(h, out), f); err != nil {
		out.Close()
		return err
	}
	if err := out.Close(); err != nil {
		return err
	}
	name := filepath.Base(dst)
	prefix := ""
	if strings.ContainsAny(name, "\\\n") {
		prefix = "\\"
		name = strings.NewReplacer("\\", "\\\\", "\n", "\\n").Replace(name)
	}
	return os.WriteFile(check, []byte(fmt.Sprintf("%s%x  %s\n", prefix, h.Sum(nil), name)), 0644)
}

func python(src, dst string) error {
	files, err := os.ReadDir(src)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(dst, 0755); err != nil {
		return err
	}
	var wheels, sdists int
	for _, f := range files {
		name := f.Name()
		switch {
		case strings.HasSuffix(name, ".whl"):
			wheels++
		case strings.HasSuffix(name, ".tar.gz"):
			sdists++
		default:
			return fmt.Errorf("unexpected Python distribution %s", name)
		}
		p := filepath.Join(dst, name)
		if err := archive(filepath.Join(src, name), p, p+".sha512"); err != nil {
			return err
		}
	}
	if wheels != 1 || sdists != 1 {
		return fmt.Errorf("expected one wheel and one source distribution in %s", src)
	}
	return nil
}

func main() {
	args := os.Args[1:]
	var err error
	switch {
	case len(args) == 4 && args[0] == "archive":
		err = archive(args[1], args[2], args[3])
	case len(args) == 3 && args[0] == "python":
		err = python(args[1], args[2])
	case len(args) == 2 && args[0] == "empty":
		err = os.MkdirAll(args[1], 0755)
	default:
		err = fmt.Errorf("usage: stage archive INPUT OUTPUT CHECKFILE, stage python INPUT OUTPUT, or stage empty OUTPUT")
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
