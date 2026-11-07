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

// sum writes a GNU sha512sum checkfile. Python outputs are copied into the
// download tree during the same pass. Native archives are already Bazel outputs.
func sum(src, check, copyTo string) error {
	f, err := os.Open(src)
	if err != nil {
		return err
	}
	defer f.Close()
	h := sha512.New()
	var w io.Writer = h
	if copyTo != "" {
		out, err := os.Create(copyTo)
		if err != nil {
			return err
		}
		w = io.MultiWriter(h, out)
		_, err = io.Copy(w, f)
		closeErr := out.Close()
		if err != nil {
			return err
		}
		if closeErr != nil {
			return closeErr
		}
	} else if _, err = io.Copy(w, f); err != nil {
		return err
	}
	name := filepath.Base(src)
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
		if !f.Type().IsRegular() {
			return fmt.Errorf("not a regular distribution: %s", name)
		}
		p := filepath.Join(dst, name)
		if err := sum(filepath.Join(src, name), p+".sha512", p); err != nil {
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
	case len(args) == 3 && args[0] == "checksum":
		err = sum(args[1], args[2], "")
	case len(args) == 3 && args[0] == "python":
		err = python(args[1], args[2])
	case len(args) == 2 && args[0] == "empty":
		err = os.MkdirAll(args[1], 0755)
	default:
		err = fmt.Errorf("usage: stage checksum|python INPUT OUTPUT, or stage empty OUTPUT")
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
