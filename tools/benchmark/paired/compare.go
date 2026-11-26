// Copyright 2026 The gVisor Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package main

import (
	"bytes"
	"context"
	"encoding/csv"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
)

func (r report) compare(ctx context.Context, expected []string, output, benchstat string, alpha, confidence float64, controlOnly bool) error {
	original := r.Commands
	for _, phase := range []string{"aa", "ab"} {
		directory := filepath.Join(output, phase)
		if err := os.Mkdir(directory, 0755); err != nil {
			return err
		}
		r.Visits = nil
		r.Commands = original
		if phase == "aa" {
			r.Commands = map[string]command{"A": original["B"], "B": original["B"]}
		}
		if err := r.record(ctx, expected, directory); err != nil {
			return err
		}
		data, err := os.ReadFile(filepath.Join(directory, "pair-0-A.bench"))
		if err != nil {
			return err
		}
		results, err := parse(data, expected, false)
		if err != nil {
			return err
		}
		metrics := make(map[string]int)
		for _, result := range results {
			metrics[result.Name.String()] = len(result.Values)
		}
		flagged := false
		for _, contrast := range []string{"A-B", "A-early-late", "B-early-late", "A-first-second", "B-first-second"} {
			args := []string{"-table=.config", "-row=.fullname", "-col=.file", "-ignore=pair,pid,arm", "-alpha=" + strconv.FormatFloat(alpha, 'g', -1, 64), "-confidence=" + strconv.FormatFloat(confidence, 'g', -1, 64)}
			for i := range 24 {
				for _, arm := range []string{"A", "B"} {
					label := arm
					switch contrast {
					case "A-early-late", "B-early-late":
						if arm != contrast[:1] {
							continue
						}
						label += "-early"
						if i >= 12 {
							label = arm + "-late"
						}
					case "A-first-second", "B-first-second":
						if arm != contrast[:1] {
							continue
						}
						label += "-first"
						if (i%2 == 0) != (arm == "A") {
							label = arm + "-second"
						}
					}
					args = append(args, label+"="+filepath.Join(directory, fmt.Sprintf("pair-%d-%s.bench", i, arm)))
				}
			}
			// Keep the named reference column first, including B-first vs B-second.
			slices.SortStableFunc(args[6:], func(a, b string) int {
				left, _, _ := strings.Cut(a, "=")
				right, _, _ := strings.Cut(b, "=")
				return strings.Compare(left, right)
			})
			for _, format := range []string{"text", "csv"} {
				cmd := exec.CommandContext(ctx, benchstat, append([]string{"-format=" + format}, args...)...)
				var stdout, stderr bytes.Buffer
				cmd.Stdout, cmd.Stderr = &stdout, &stderr
				runErr := cmd.Run()
				path := filepath.Join(directory, contrast+"."+format)
				if err := errors.Join(runErr, os.WriteFile(path, stdout.Bytes(), 0644), os.WriteFile(path+".stderr", stderr.Bytes(), 0644)); err != nil {
					return fmt.Errorf("%s: %w: %s", contrast, err, stderr.Bytes())
				}
				if format == "csv" {
					flags, err := comparisonFlags(stdout.Bytes(), metrics)
					if err != nil {
						return fmt.Errorf("%s: %w", contrast, err)
					}
					flagged = flagged || flags
				}
			}
		}
		decision := struct {
			Alpha      float64 `json:"alpha"`
			Confidence float64 `json:"confidence"`
			Flagged    bool    `json:"flagged"`
		}{alpha, confidence, flagged}
		data, err = json.MarshalIndent(decision, "", "  ")
		if err != nil {
			return err
		}
		if err := os.WriteFile(filepath.Join(directory, "comparison.json"), data, 0644); err != nil {
			return err
		}
		if phase == "aa" && flagged {
			fmt.Println("MATCHED_AA_HOLD_AB_NOT_RUN")
			return nil
		}
		fmt.Printf("MATCHED_%s_COMPLETE\n", phase)
		if controlOnly {
			return nil
		}
	}
	return nil
}

func comparisonFlags(data []byte, metrics map[string]int) (bool, error) {
	reader := csv.NewReader(bytes.NewReader(data))
	reader.FieldsPerRecord = -1
	rows, err := reader.ReadAll()
	if err != nil {
		return false, err
	}
	counts := make(map[string]int)
	flagged := false
	for _, row := range rows {
		if len(row) != 7 || metrics[row[0]] == 0 {
			continue
		}
		counts[row[0]]++
		flagged = flagged || row[5] != "~"
	}
	for name, count := range metrics {
		if counts[name] != count {
			return false, fmt.Errorf("%s: got %d comparison rows, want %d", name, counts[name], count)
		}
	}
	return flagged, nil
}
