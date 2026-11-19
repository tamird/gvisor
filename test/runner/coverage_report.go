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

package main

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
)

// runscCommand keeps disabled coverage from using the environment fallback.
func runscCommand(executable string, collect bool, args ...string) *exec.Cmd {
	cmd := exec.Command(executable, args...)
	if !collect {
		cmd.Env = filterEnv(os.Environ(), []string{"GO_COVERAGE_FILE"})
	}
	return cmd
}

func checkCoverageExpectation(expectation string, collect bool) error {
	switch expectation {
	case "":
		return nil
	case "required":
		if collect {
			return nil
		}
	case "absent":
		if !collect {
			return nil
		}
	default:
		return fmt.Errorf("unknown coverage report expectation %q", expectation)
	}
	return fmt.Errorf("coverage report expectation %q conflicts with collect-coverage=%t", expectation, collect)
}

type coverageFileMetadata struct {
	Name                     string `json:"name"`
	SHA256                   string `json:"sha256"`
	Positions                int    `json:"positions"`
	SentryPositions          int    `json:"sentry_positions"`
	GeneratedSentryPositions int    `json:"generated_sentry_positions"`
}

type coverageReportMetadata struct {
	Test                     string                 `json:"test"`
	Directory                string                 `json:"directory"`
	Expectation              string                 `json:"expectation"`
	Files                    []coverageFileMetadata `json:"files"`
	Positions                int                    `json:"positions"`
	SentryPositions          int                    `json:"sentry_positions"`
	GeneratedSentryPositions int                    `json:"generated_sentry_positions"`
	PrimaryError             string                 `json:"primary_error,omitempty"`
	ValidationError          string                 `json:"validation_error,omitempty"`
}

type coverageReport struct {
	dir      string
	reports  string
	metadata coverageReportMetadata
}

func prepareCoverageReport(outputs, test, expectation string, collect bool) (*coverageReport, error) {
	if err := checkCoverageExpectation(expectation, collect); err != nil {
		return nil, err
	}
	if expectation == "" {
		return nil, nil
	}
	if !filepath.IsAbs(outputs) {
		return nil, fmt.Errorf("coverage report validation requires an absolute TEST_UNDECLARED_OUTPUTS_DIR")
	}
	// A fresh sibling of the test log directory survives successful log cleanup.
	dir, err := os.MkdirTemp(outputs, "coverage-")
	if err != nil {
		return nil, err
	}
	reports := filepath.Join(dir, "reports")
	if err := os.Mkdir(reports, 0700); err != nil {
		return nil, err
	}
	return &coverageReport{
		dir:      dir,
		reports:  reports,
		metadata: coverageReportMetadata{Test: test, Directory: filepath.Base(dir), Expectation: expectation, Files: []coverageFileMetadata{}},
	}, nil
}

// coverageSource parses Report's source positions, which do not include PCs.
func coverageSource(line string) (string, error) {
	separator := strings.LastIndexByte(line, ':')
	if separator < 0 {
		return "", fmt.Errorf("invalid coverage source position %q", line)
	}
	file, coordinates := line[:separator], line[separator+1:]
	if !strings.HasSuffix(file, ".go") || path.Clean(file) != file {
		return "", fmt.Errorf("invalid coverage source position %q", line)
	}
	start, end, ok := strings.Cut(coordinates, ",")
	if !ok {
		return "", fmt.Errorf("invalid coverage coordinates %q", line)
	}
	parse := func(value string) (int, int, error) {
		line, column, ok := strings.Cut(value, ".")
		if !ok {
			return 0, 0, fmt.Errorf("invalid coverage coordinate %q", value)
		}
		l, err := strconv.Atoi(line)
		if err != nil || l <= 0 || strconv.Itoa(l) != line {
			return 0, 0, fmt.Errorf("invalid coverage line %q", line)
		}
		c, err := strconv.Atoi(column)
		if err != nil || c <= 0 || strconv.Itoa(c) != column {
			return 0, 0, fmt.Errorf("invalid coverage column %q", column)
		}
		return l, c, nil
	}
	sl, sc, err := parse(start)
	if err != nil {
		return "", err
	}
	el, ec, err := parse(end)
	if err != nil {
		return "", err
	}
	if el < sl || (el == sl && ec < sc) {
		return "", fmt.Errorf("reversed coverage position %q", line)
	}
	if strings.HasPrefix(file, "gvisor.dev/gvisor/pkg/coverage/") {
		return "", fmt.Errorf("excluded coverage runtime position %q", line)
	}
	return file, nil
}

func (r *coverageReport) validate() error {
	err := filepath.WalkDir(r.reports, func(name string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if entry.IsDir() {
			return nil
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if !info.Mode().IsRegular() {
			return fmt.Errorf("coverage report is not a regular file: %s", name)
		}
		if r.metadata.Expectation == "absent" {
			return fmt.Errorf("unexpected coverage report: %s", name)
		}
		f, err := os.OpenFile(name, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
		if err != nil {
			return err
		}
		defer f.Close()
		opened, err := f.Stat()
		if err != nil || !os.SameFile(info, opened) {
			return fmt.Errorf("coverage report changed while opening %s: %v", name, err)
		}
		hash := sha256.New()
		scanner := bufio.NewScanner(io.TeeReader(f, hash))
		relative, err := filepath.Rel(r.reports, name)
		if err != nil {
			return err
		}
		metadata := coverageFileMetadata{Name: relative}
		for scanner.Scan() {
			file, err := coverageSource(scanner.Text())
			if err != nil {
				return fmt.Errorf("%s: %w", relative, err)
			}
			metadata.Positions++
			if strings.HasPrefix(file, "gvisor.dev/gvisor/pkg/sentry/") {
				metadata.SentryPositions++
				if strings.HasSuffix(file, ".pb.go") {
					metadata.GeneratedSentryPositions++
				}
			}
		}
		if err := scanner.Err(); err != nil {
			return err
		}
		metadata.SHA256 = hex.EncodeToString(hash.Sum(nil))
		r.metadata.Files = append(r.metadata.Files, metadata)
		r.metadata.Positions += metadata.Positions
		r.metadata.SentryPositions += metadata.SentryPositions
		r.metadata.GeneratedSentryPositions += metadata.GeneratedSentryPositions
		return nil
	})
	if err != nil {
		return err
	}
	if r.metadata.Expectation == "required" && (r.metadata.SentryPositions == 0 || r.metadata.GeneratedSentryPositions == 0) {
		return fmt.Errorf("coverage report has %d Sentry positions and %d generated Sentry positions, want both nonzero", r.metadata.SentryPositions, r.metadata.GeneratedSentryPositions)
	}
	return nil
}

func (r *coverageReport) finish(primary error) error {
	validation := r.validate()
	if primary != nil {
		r.metadata.PrimaryError = primary.Error()
	}
	if validation != nil {
		r.metadata.ValidationError = validation.Error()
	}
	f, err := os.OpenFile(filepath.Join(r.dir, "metadata.json"), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err == nil {
		err = json.NewEncoder(io.MultiWriter(f, os.Stdout)).Encode(r.metadata)
		err = errors.Join(err, f.Close())
	}
	return errors.Join(primary, validation, err)
}

// filterEnv returns an environment with the excluded variables removed.
func filterEnv(env, exclude []string) []string {
	var out []string
	for _, kv := range env {
		ok := true
		for _, k := range exclude {
			if strings.HasPrefix(kv, k+"=") {
				ok = false
				break
			}
		}
		if ok {
			out = append(out, kv)
		}
	}
	return out
}
