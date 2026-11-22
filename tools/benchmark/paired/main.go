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

// paired runs counterbalanced Go benchmark processes and checks repeatability.
package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"maps"
	"math"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"slices"
	"syscall"
	"time"

	"golang.org/x/perf/benchfmt"
	"golang.org/x/perf/benchmath"
)

type command struct {
	Argv   []string `json:"argv"`
	SHA256 string   `json:"sha256"`
}

type result struct {
	Metrics map[string]float64 `json:"metrics"`
	Config  map[string]string `json:"config"`
}

type visit struct {
	Pair    int               `json:"pair"`
	Arm     string            `json:"arm"`
	PID     int               `json:"pid"`
	Results map[string]result `json:"results"`
}

type interval struct {
	Median     float64 `json:"median"`
	Low        float64 `json:"low"`
	High       float64 `json:"high"`
	Confidence float64 `json:"confidence"`
}

type contrasts struct {
	Ratio interval `json:"ratio"`
	Drift interval `json:"drift"`
	Order interval `json:"order"`
}

type report struct {
	Preflight bool                 `json:"preflight"`
	Compare   bool                 `json:"compare"`
	Platform  string               `json:"platform"`
	Commands  map[string]command   `json:"commands"`
	Visits    []visit              `json:"visits"`
	Cases     map[string]contrasts `json:"cases,omitempty"`
	Error     string               `json:"error,omitempty"`
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run() error {
	a := flag.String("a", "", "JSON argv for A; defaults to the Bazel test command")
	b := flag.String("b", "", "JSON argv for B; defaults to the Bazel test command")
	cases := flag.String("cases", "", "JSON map from Bazel test labels to canonical benchfmt names")
	preflight := flag.Bool("preflight", false, "check two pairs with one-iteration results, without statistical gates")
	compare := flag.Bool("compare", false, "report the ratio interval without requiring A/A equivalence; retain drift and order gates")
	flag.Parse()
	var inventory map[string][]string
	if err := json.Unmarshal([]byte(*cases), &inventory); err != nil {
		return fmt.Errorf("cases: %w", err)
	}
	expected := inventory[os.Getenv("TEST_TARGET")]
	if len(expected) == 0 {
		return fmt.Errorf("no benchmark inventory for %q", os.Getenv("TEST_TARGET"))
	}
	for i, name := range expected {
		if name == "" || slices.Contains(expected[:i], name) {
			return fmt.Errorf("empty or duplicate inventory entry %q", name)
		}
	}
	output := os.Getenv("TEST_UNDECLARED_OUTPUTS_DIR")
	if output == "" {
		return fmt.Errorf("TEST_UNDECLARED_OUTPUTS_DIR must name the Bazel output directory")
	}
	r := report{Preflight: *preflight, Compare: *compare, Platform: runtime.GOOS + "/" + runtime.GOARCH, Commands: make(map[string]command)}
	for arm, raw := range map[string]string{"A": *a, "B": *b} {
		argv := slices.Clone(flag.Args())
		if raw != "" {
			if err := json.Unmarshal([]byte(raw), &argv); err != nil {
				return fmt.Errorf("%s argv: %w", arm, err)
			}
		}
		if len(argv) == 0 {
			return fmt.Errorf("%s has no executable", arm)
		}
		f, err := os.Open(argv[0])
		if err != nil {
			return err
		}
		h := sha256.New()
		_, hashErr := io.Copy(h, f)
		if err := errors.Join(hashErr, f.Close()); err != nil {
			return err
		}
		r.Commands[arm] = command{argv, hex.EncodeToString(h.Sum(nil))}
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	err := r.collect(ctx, expected)
	if err != nil {
		r.Error = err.Error()
	}
	f, openErr := os.Create(filepath.Join(output, "paired.json"))
	if openErr != nil {
		return errors.Join(err, openErr)
	}
	return errors.Join(err, json.NewEncoder(f).Encode(r), f.Close())
}

func (r *report) collect(ctx context.Context, expected []string) error {
	var reference map[string]result
	for i, order := range schedule(r.Preflight) {
		pair := i - 1
		for _, arm := range order {
			v := visit{Pair: pair, Arm: string(arm)}
			argv := r.Commands[v.Arm].Argv
			cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
			cmd.WaitDelay = 5 * time.Second
			var out bytes.Buffer
			cmd.Stdout, cmd.Stderr = &out, os.Stderr
			err := cmd.Run()
			if cmd.ProcessState != nil {
				v.PID = cmd.ProcessState.Pid()
			}
			fmt.Printf("PAIR %d ARM %s PID %d\n%s", pair, v.Arm, v.PID, out.Bytes())
			if err == nil {
				v.Results, err = parse(out.Bytes(), expected, r.Preflight)
			}
			if err == nil && reference != nil {
				for name, value := range v.Results {
					previous := reference[name]
					if !maps.Equal(previous.Config, value.Config) || len(previous.Metrics) != len(value.Metrics) {
						err = fmt.Errorf("%s: benchmark platform or metric units changed", name)
						break
					}
					for unit := range previous.Metrics {
						if _, ok := value.Metrics[unit]; !ok {
							err = fmt.Errorf("%s: metric unit %q disappeared", name, unit)
						}
					}
				}
			}
			if reference == nil {
				reference = v.Results
			}
			r.Visits = append(r.Visits, v)
			if err != nil {
				return fmt.Errorf("pair %d arm %s: %w", pair, v.Arm, err)
			}
		}
	}
	if r.Preflight {
		return nil
	}
	r.Cases = make(map[string]contrasts)
	var failures []error
	for _, name := range expected {
		var a, b []float64
		for _, v := range r.Visits {
			if v.Pair < 0 {
				continue
			}
			x := v.Results[name].Metrics["sec/op"]
			if v.Arm == "A" {
				a = append(a, x)
			} else {
				b = append(b, x)
			}
		}
		c, err := summarize(a, b)
		if err != nil {
			failures = append(failures, fmt.Errorf("%s: %w", name, err))
			continue
		}
		r.Cases[name] = c
		checks := []interval{c.Drift, c.Order}
		if !r.Compare {
			checks = append(checks, c.Ratio)
		}
		for _, x := range checks {
			if x.Low < .995 || x.High > 1.005 || x.Confidence < .95 {
				failures = append(failures, fmt.Errorf("%s: repeatability interval outside [0.995, 1.005]: %+v", name, c))
				break
			}
		}
	}
	return errors.Join(failures...)
}

func schedule(preflight bool) []string {
	pairs := 24
	if preflight {
		pairs = 2
	}
	orders := []string{"AB"} // Discard one warmup pair.
	for i := range pairs {
		orders = append(orders, []string{"AB", "BA"}[i%2])
	}
	return orders
}

func parse(out []byte, expected []string, preflight bool) (map[string]result, error) {
	found := make(map[string]result)
	reader := benchfmt.NewReader(bytes.NewReader(out), "stdout")
	for reader.Scan() {
		switch x := reader.Result().(type) {
		case *benchfmt.SyntaxError:
			return nil, x
		case *benchfmt.Result:
			name := x.Name.String()
			if _, duplicate := found[name]; duplicate || !slices.Contains(expected, name) {
				return nil, fmt.Errorf("unexpected or duplicate benchmark %q", name)
			}
			if preflight && x.Iters != 1 {
				return nil, fmt.Errorf("%s: preflight requires one iteration, got %d", name, x.Iters)
			}
			seconds, ok := x.Value("sec/op")
			if !ok || seconds <= 0 || math.IsNaN(seconds) || math.IsInf(seconds, 0) {
				return nil, fmt.Errorf("%s: missing or invalid timing", name)
			}
			v := result{Metrics: make(map[string]float64), Config: make(map[string]string)}
			for _, metric := range x.Values {
				if _, duplicate := v.Metrics[metric.Unit]; duplicate || math.IsNaN(metric.Value) || math.IsInf(metric.Value, 0) {
					return nil, fmt.Errorf("%s: duplicate or invalid metric %q", name, metric.Unit)
				}
				v.Metrics[metric.Unit] = metric.Value
			}
			for _, key := range []string{"goos", "goarch", "cpu"} {
				v.Config[key] = x.GetConfig(key)
			}
			if v.Config["goos"] != runtime.GOOS || v.Config["goarch"] != runtime.GOARCH || v.Config["cpu"] == "" {
				return nil, fmt.Errorf("%s: benchmark platform does not match runner %s/%s: %v", name, runtime.GOOS, runtime.GOARCH, v.Config)
			}
			found[name] = v
		}
	}
	if err := reader.Err(); err != nil {
		return nil, err
	}
	if len(found) != len(expected) {
		return nil, fmt.Errorf("got %d benchmark rows, want %d", len(found), len(expected))
	}
	return found, nil
}

func summarize(a, b []float64) (contrasts, error) {
	if len(a) != 24 || len(b) != 24 {
		return contrasts{}, fmt.Errorf("need 24 matched pairs")
	}
	ratio := make([]float64, 24)
	var drift, order []float64
	for i := range ratio {
		ratio[i] = b[i] / a[i]
	}
	for i := range 12 {
		drift = append(drift, math.Sqrt(a[i+12]*b[i+12])/math.Sqrt(a[i]*b[i]))
		order = append(order, ratio[2*i]/ratio[2*i+1])
	}
	var c contrasts
	intervals := []*interval{&c.Ratio, &c.Drift, &c.Order}
	for i, values := range [][]float64{ratio, drift, order} {
		x := benchmath.AssumeNothing.Summary(benchmath.NewSample(values, &benchmath.DefaultThresholds), .95)
		if len(x.Warnings) != 0 {
			return c, fmt.Errorf("confidence interval: %v", x.Warnings)
		}
		if math.IsNaN(x.Lo) || math.IsNaN(x.Hi) || math.IsInf(x.Lo, 0) || math.IsInf(x.Hi, 0) {
			return c, fmt.Errorf("non-finite confidence interval: %+v", x)
		}
		*intervals[i] = interval{x.Center, x.Lo, x.Hi, x.Confidence}
	}
	return c, nil
}
