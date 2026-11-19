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
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const sentryReport = "gvisor.dev/gvisor/pkg/sentry/kernel/task.go:12.3,13.4\n" +
	"gvisor.dev/gvisor/pkg/sentry/arch/registers_go_proto/registers.pb.go:4.1,4.8\n"

func TestCoverageReportValidation(t *testing.T) {
	for _, tc := range []struct {
		name      string
		mode      string
		files     map[string]string
		symlink   bool
		wantError bool
	}{
		{name: "required", mode: "required", files: map[string]string{"sentry": sentryReport, "controller": ""}},
		{name: "non-module source", mode: "required", files: map[string]string{"report": sentryReport + "example.org/other/source.go:7.2,8.1\n"}},
		{name: "colon source path", mode: "required", files: map[string]string{"report": sentryReport + "C:/source/file.go:7.2,8.1\n"}},
		{name: "missing", mode: "required", wantError: true},
		{name: "empty", mode: "required", files: map[string]string{"empty": ""}, wantError: true},
		{name: "PC pairs", mode: "required", files: map[string]string{"report": "0x123\n" + sentryReport}, wantError: true},
		{name: "zero coordinate", mode: "required", files: map[string]string{"report": strings.ReplaceAll(sentryReport, "12.3", "0.3")}, wantError: true},
		{name: "reversed", mode: "required", files: map[string]string{"report": strings.ReplaceAll(sentryReport, "12.3,13.4", "13.4,12.3")}, wantError: true},
		{name: "malformed", mode: "required", files: map[string]string{"report": sentryReport + "gvisor.dev/gvisor/pkg/sentry/a.go:1.2,3.4junk\n"}, wantError: true},
		{name: "excluded", mode: "required", files: map[string]string{"report": sentryReport + "gvisor.dev/gvisor/pkg/coverage/coverage.go:1.1,2.1\n"}, wantError: true},
		{name: "no Sentry", mode: "required", files: map[string]string{"controller": "gvisor.dev/gvisor/runsc/main.go:1.1,2.1\n"}, wantError: true},
		{name: "no protobuf", mode: "required", files: map[string]string{"sentry": "gvisor.dev/gvisor/pkg/sentry/kernel/task.go:1.1,2.1\n"}, wantError: true},
		{name: "symlink", mode: "required", symlink: true, wantError: true},
		{name: "off", mode: "absent"},
		{name: "off unexpected", mode: "absent", files: map[string]string{"empty": ""}, wantError: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, err := prepareCoverageReport(t.TempDir(), tc.name, tc.mode, tc.mode == "required")
			if err != nil {
				t.Fatal(err)
			}
			for name, data := range tc.files {
				if err := os.WriteFile(filepath.Join(r.reports, name), []byte(data), 0600); err != nil {
					t.Fatal(err)
				}
			}
			if tc.symlink {
				outside := filepath.Join(t.TempDir(), "report")
				if err := os.WriteFile(outside, []byte(sentryReport), 0600); err != nil {
					t.Fatal(err)
				}
				if err := os.Symlink(outside, filepath.Join(r.reports, "alias")); err != nil {
					t.Fatal(err)
				}
			}
			err = r.finish(nil)
			if (err != nil) != tc.wantError {
				t.Fatalf("finish() = %v, want error %t", err, tc.wantError)
			}
			data, err := os.ReadFile(filepath.Join(r.dir, "metadata.json"))
			if err != nil {
				t.Fatal(err)
			}
			var metadata coverageReportMetadata
			if err := json.Unmarshal(data, &metadata); err != nil {
				t.Fatal(err)
			}
			if (metadata.ValidationError != "") != tc.wantError {
				t.Fatalf("metadata validation error = %q, want error %t", metadata.ValidationError, tc.wantError)
			}
		})
	}
}

func TestCoverageReportRetention(t *testing.T) {
	outputs := t.TempDir()
	logs := filepath.Join(outputs, "UnameTest.Uname")
	if err := os.Mkdir(logs, 0700); err != nil {
		t.Fatal(err)
	}
	r, err := prepareCoverageReport(outputs, "UnameTest.Uname", "required", true)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(r.reports, "sentry"), []byte(sentryReport), 0600); err != nil {
		t.Fatal(err)
	}
	if err := r.finish(nil); err != nil {
		t.Fatal(err)
	}
	if err := os.RemoveAll(logs); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(filepath.Join(r.dir, "metadata.json"))
	if err != nil {
		t.Fatal(err)
	}
	var metadata coverageReportMetadata
	if err := json.Unmarshal(data, &metadata); err != nil {
		t.Fatal(err)
	}
	if len(metadata.Files) != 1 || metadata.Positions != 2 || metadata.SentryPositions != 2 || metadata.GeneratedSentryPositions != 1 {
		t.Fatalf("metadata = %+v, want one file, two Sentry positions and one protobuf position", metadata)
	}
	retained, err := os.ReadFile(filepath.Join(r.reports, metadata.Files[0].Name))
	if err != nil {
		t.Fatal(err)
	}
	if string(retained) != sentryReport {
		t.Fatalf("retained report = %q, want %q", retained, sentryReport)
	}
	hash := sha256.Sum256(retained)
	if got := metadata.Files[0].SHA256; got != hex.EncodeToString(hash[:]) {
		t.Fatalf("report SHA256 = %s, want %x", got, hash)
	}
}

func TestCoveragePrimaryFailure(t *testing.T) {
	for _, tc := range []struct {
		name      string
		hasReport bool
	}{
		{name: "valid report", hasReport: true},
		{name: "missing report"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, err := prepareCoverageReport(t.TempDir(), "failure", "required", true)
			if err != nil {
				t.Fatal(err)
			}
			if tc.hasReport {
				if err := os.WriteFile(filepath.Join(r.reports, "sentry"), []byte(sentryReport), 0600); err != nil {
					t.Fatal(err)
				}
			}
			primary := errors.New("sandbox failed")
			if err := r.finish(primary); !errors.Is(err, primary) {
				t.Fatalf("finish() = %v, want primary failure %v", err, primary)
			}
			data, err := os.ReadFile(filepath.Join(r.dir, "metadata.json"))
			if err != nil {
				t.Fatal(err)
			}
			var metadata coverageReportMetadata
			if err := json.Unmarshal(data, &metadata); err != nil {
				t.Fatal(err)
			}
			wantValidationError := !tc.hasReport
			if metadata.PrimaryError != primary.Error() || (metadata.ValidationError != "") != wantValidationError {
				t.Fatalf("metadata = %+v, want primary failure and validation error %t", metadata, wantValidationError)
			}
		})
	}
}

func TestCoverageReportConfiguration(t *testing.T) {
	for _, tc := range []struct {
		mode    string
		collect bool
		outputs string
	}{
		{mode: "invalid", collect: true, outputs: t.TempDir()},
		{mode: "required", collect: false, outputs: t.TempDir()},
		{mode: "absent", collect: true, outputs: t.TempDir()},
		{mode: "required", collect: true},
		{mode: "required", collect: true, outputs: "relative"},
	} {
		if _, err := prepareCoverageReport(tc.outputs, "test", tc.mode, tc.collect); err == nil {
			t.Errorf("prepareCoverageReport(%+v) succeeded, want error", tc)
		}
	}
	if r, err := prepareCoverageReport("", "test", "", true); r != nil || err != nil {
		t.Fatalf("ordinary mode = (%v, %v), want (nil, nil)", r, err)
	}
}

func TestRunscCoverageEnvironment(t *testing.T) {
	t.Setenv("GO_COVERAGE_FILE", "/unowned/report")
	t.Setenv("RUNNER_COVERAGE_TEST", "preserved")
	for _, collect := range []bool{false, true} {
		cmd := runscCommand("runsc", collect, "wait", "test")
		found := false
		preserved := false
		for _, entry := range cmd.Environ() {
			found = found || strings.HasPrefix(entry, "GO_COVERAGE_FILE=")
			preserved = preserved || entry == "RUNNER_COVERAGE_TEST=preserved"
		}
		if found != collect || !preserved {
			t.Errorf("collect=%t: fallback present=%t, other environment preserved=%t", collect, found, preserved)
		}
	}
}
