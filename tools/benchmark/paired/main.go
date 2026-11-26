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

// paired records counterbalanced Go benchmark processes. Successful execution
// validates the captured data, but does not establish measurement stability.
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
	"math"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"syscall"
	"time"

	"golang.org/x/perf/benchfmt"
)

type command struct {
	SHA256 string   `json:"sha256"`
	Argv   []string `json:"argv"`
}

type visit struct {
	ExitCode *int      `json:"exit_code,omitempty"`
	Affinity *affinity `json:"affinity,omitempty"`
	Arm      string    `json:"arm"`
	Output   string    `json:"output"`
	Pair     int       `json:"pair"`
	PID      int       `json:"pid"`
}

type affinity struct {
	Allowed []int `json:"allowed_cpus"`
	Launch  []int `json:"launch_thread_cpus,omitempty"`
}

type report struct {
	reference    map[string]*benchfmt.Result
	Commands     map[string]command `json:"commands"`
	Units        []string           `json:"units,omitempty"`
	Platform     string             `json:"platform"`
	Error        string             `json:"error,omitempty"`
	Visits       []visit            `json:"visits"`
	PinCPU       int                `json:"pin_cpu"`
	VisitTimeout time.Duration      `json:"visit_timeout_ns"`
	Iterations   int                `json:"iterations,omitempty"`
	Preflight    bool               `json:"preflight"`
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run() error {
	flag.Usage = func() {
		fmt.Fprintln(flag.CommandLine.Output(), "Record fresh Go benchmark processes. Exit status reports execution and data integrity, not measurement stability.")
		flag.PrintDefaults()
	}
	a := flag.String("a", "", "JSON argv for A; defaults to the Bazel test command")
	b := flag.String("b", "", "JSON argv for B; defaults to the Bazel test command")
	cases := flag.String("cases", "", "JSON map from Bazel test labels to canonical benchfmt names")
	casesFile := flag.String("cases-file", "", "file containing the cases JSON instead of -cases")
	benchstat := flag.String("benchstat", "", "benchstat executable; collect A/A then conditional A/B when set")
	controlOnly := flag.Bool("control-only", false, "collect and report A/A only; requires benchstat")
	validate := flag.Bool("validate", false, "validate inputs without starting benchmark processes")
	alpha := flag.Float64("alpha", 0.01, "predeclared per-contrast significance threshold")
	confidence := flag.Float64("confidence", 0.95, "confidence level for benchstat estimates")
	preflight := flag.Bool("preflight", false, "validate two pairs with one-iteration results")
	pinCPU := flag.Int("pin-cpu", -1, "bind children to this allowed logical CPU; -1 leaves affinity unchanged")
	visitTimeout := flag.Duration("visit-timeout", 0, "maximum wall-clock duration of one benchmark process; required")
	iterations := flag.Int("iterations", 0, "required iterations in every result; zero permits any positive count")
	units := flag.String("metric-units", "", "comma-separated complete metric-unit inventory; empty permits any fixed set")
	flag.Parse()
	if *pinCPU < -1 {
		return fmt.Errorf("pin-cpu must be -1 or a non-negative logical CPU")
	}
	if *visitTimeout <= 0 {
		return fmt.Errorf("visit-timeout must be positive")
	}
	if *iterations < 0 {
		return fmt.Errorf("iterations must be non-negative")
	}
	if *controlOnly && *benchstat == "" {
		return fmt.Errorf("control-only requires benchstat")
	}
	if !(*alpha > 0 && *alpha < 1 && *confidence > 0 && *confidence < 1) {
		return fmt.Errorf("alpha and confidence must be between zero and one")
	}
	if *casesFile != "" {
		if *cases != "" {
			return fmt.Errorf("cases and cases-file are mutually exclusive")
		}
		data, err := os.ReadFile(*casesFile)
		if err != nil {
			return err
		}
		*cases = string(data)
	}
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
	r := report{
		Preflight:    *preflight,
		PinCPU:       *pinCPU,
		VisitTimeout: *visitTimeout,
		Iterations:   *iterations,
		Platform:     runtime.GOOS + "/" + runtime.GOARCH,
		Commands:     make(map[string]command),
	}
	if *units != "" {
		r.Units = strings.Split(*units, ",")
		for i, unit := range r.Units {
			if unit == "" || slices.Contains(r.Units[:i], unit) {
				return fmt.Errorf("empty or duplicate metric unit %q", unit)
			}
		}
	}
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
		r.Commands[arm] = command{hex.EncodeToString(h.Sum(nil)), argv}
	}
	if *benchstat != "" {
		if _, err := exec.LookPath(*benchstat); err != nil {
			return err
		}
	}
	if *validate {
		return nil
	}
	if *controlOnly {
		r.Commands["A"] = r.Commands["B"]
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if *benchstat != "" && !r.Preflight {
		return r.compare(ctx, expected, output, *benchstat, *alpha, *confidence, *controlOnly)
	}
	return r.record(ctx, expected, output)
}

func (r *report) record(ctx context.Context, expected []string, output string) error {
	err := r.collect(ctx, expected, output)
	if err != nil {
		r.Error = err.Error()
	}
	f, openErr := os.Create(filepath.Join(output, "paired.json"))
	if openErr != nil {
		return errors.Join(err, openErr)
	}
	return errors.Join(err, json.NewEncoder(f).Encode(r), f.Close())
}

func startChild(cmd *exec.Cmd, cpu int) (*affinity, error) {
	if cpu == -1 {
		return nil, cmd.Start()
	}
	a := new(affinity)
	done := make(chan error)
	go func() {
		// StartProcess inherits this locked thread's affinity. The runtime
		// terminates the thread when this goroutine exits without unlocking it.
		runtime.LockOSThread()
		var err error
		defer func() { done <- err }()
		a.Allowed, err = getAffinity(0)
		if err != nil {
			err = fmt.Errorf("read launch-thread affinity: %w", err)
			return
		}
		if !slices.Contains(a.Allowed, cpu) {
			err = fmt.Errorf("requested CPU %d is outside allowed CPUs %v", cpu, a.Allowed)
			return
		}
		if err = setAffinity(cpu); err != nil {
			err = fmt.Errorf("pin launch thread to CPU %d: %w", cpu, err)
			return
		}
		a.Launch, err = getAffinity(0)
		if err != nil {
			err = fmt.Errorf("observe launch-thread affinity: %w", err)
			return
		}
		if !slices.Equal(a.Launch, []int{cpu}) {
			err = fmt.Errorf("launch-thread CPUs %v, want [%d]", a.Launch, cpu)
			return
		}
		err = cmd.Start()
	}()
	return a, <-done
}

func (r *report) collect(ctx context.Context, expected []string, output string) error {
	for i, order := range schedule(r.Preflight) {
		pair := i - 1
		for _, arm := range order {
			v := visit{Pair: pair, Arm: string(arm)}
			argv := r.Commands[v.Arm].Argv
			visitCtx, cancel := context.WithTimeout(ctx, r.VisitTimeout)
			cmd := exec.CommandContext(visitCtx, argv[0], argv[1:]...)
			cmd.WaitDelay = 5 * time.Second
			var out bytes.Buffer
			cmd.Stdout, cmd.Stderr = &out, os.Stderr
			var err error
			v.Affinity, err = startChild(cmd, r.PinCPU)
			if err == nil {
				err = cmd.Wait()
			}
			err = errors.Join(err, visitCtx.Err())
			cancel()
			if cmd.ProcessState != nil {
				v.PID = cmd.ProcessState.Pid()
				exitCode := cmd.ProcessState.ExitCode()
				v.ExitCode = &exitCode
			}
			v.Output = fmt.Sprintf("pair-%d-%s.bench", pair, v.Arm)
			writeErr := os.WriteFile(filepath.Join(output, v.Output), out.Bytes(), 0644)
			_, logErr := fmt.Printf("pair %d arm %s pid %d\n%s\n", pair, v.Arm, v.PID, out.Bytes())
			err = errors.Join(err, writeErr, logErr)
			var results map[string]*benchfmt.Result
			if err == nil {
				results, err = parse(out.Bytes(), expected, r.Preflight)
			}
			if err == nil && r.Iterations != 0 {
				for name, result := range results {
					if result.Iters != r.Iterations {
						err = fmt.Errorf("%s: got %d iterations, want %d", name, result.Iters, r.Iterations)
						break
					}
				}
			}
			if err == nil && len(r.Units) != 0 {
				for name, result := range results {
					if len(result.Values) != len(r.Units) {
						err = fmt.Errorf("%s: metric inventory changed: %v", name, result.Values)
						break
					}
					for _, unit := range r.Units {
						if _, ok := result.Value(unit); !ok {
							err = fmt.Errorf("%s: missing metric %q", name, unit)
						}
					}
				}
			}
			if err == nil && r.reference != nil {
				for name, value := range results {
					previous := r.reference[name]
					if !bytes.Equal(previous.Name.Full(), value.Name.Full()) {
						err = fmt.Errorf("%s: benchmark CPU configuration changed from %q to %q", name, previous.Name, value.Name)
						break
					}
					if len(previous.Config) != len(value.Config) || len(previous.Values) != len(value.Values) {
						err = fmt.Errorf("%s: benchmark platform or metric units changed", name)
						break
					}
					for _, config := range previous.Config {
						if !slices.ContainsFunc(value.Config, func(other benchfmt.Config) bool {
							return config.Key == other.Key && bytes.Equal(config.Value, other.Value)
						}) {
							err = fmt.Errorf("%s: benchmark configuration %q changed", name, config.Key)
						}
					}
					for _, metric := range previous.Values {
						if _, ok := value.Value(metric.Unit); !ok {
							err = fmt.Errorf("%s: metric unit %q disappeared", name, metric.Unit)
						}
					}
				}
			}
			if r.reference == nil {
				r.reference = results
			}
			r.Visits = append(r.Visits, v)
			if err != nil {
				return fmt.Errorf("pair %d arm %s: %w", pair, v.Arm, err)
			}
		}
	}
	return nil
}

func schedule(preflight bool) []string {
	pairs := 24
	if preflight {
		pairs = 2
	}
	orders := []string{"AB"} // Begin with one warmup pair.
	for i := range pairs {
		orders = append(orders, []string{"AB", "BA"}[i%2])
	}
	return orders
}

func parse(out []byte, expected []string, preflight bool) (map[string]*benchfmt.Result, error) {
	found := make(map[string]*benchfmt.Result)
	reader := benchfmt.NewReader(bytes.NewReader(out), "stdout")
	for reader.Scan() {
		switch x := reader.Result().(type) {
		case *benchfmt.SyntaxError:
			return nil, x
		case *benchfmt.Result:
			name := x.Name.String()
			_, parts := x.Name.Parts()
			if len(parts) > 0 && parts[len(parts)-1][0] == '-' {
				name = name[:len(name)-len(parts[len(parts)-1])]
			}
			if _, duplicate := found[name]; duplicate || !slices.Contains(expected, name) {
				return nil, fmt.Errorf("unexpected or duplicate benchmark %q", name)
			}
			if x.Iters <= 0 || preflight && x.Iters != 1 {
				return nil, fmt.Errorf("%s: unexpected iteration count %d", name, x.Iters)
			}
			seconds, ok := x.Value("sec/op")
			if !ok || seconds <= 0 || math.IsNaN(seconds) || math.IsInf(seconds, 0) {
				return nil, fmt.Errorf("%s: missing or invalid timing", name)
			}
			for i, metric := range x.Values {
				if slices.ContainsFunc(x.Values[:i], func(v benchfmt.Value) bool { return v.Unit == metric.Unit }) || math.IsNaN(metric.Value) || math.IsInf(metric.Value, 0) {
					return nil, fmt.Errorf("%s: duplicate or invalid metric %q", name, metric.Unit)
				}
			}
			if x.GetConfig("goos") != runtime.GOOS || x.GetConfig("goarch") != runtime.GOARCH {
				return nil, fmt.Errorf("%s: benchmark platform does not match runner %s/%s: %v", name, runtime.GOOS, runtime.GOARCH, x.Config)
			}
			found[name] = x.Clone()
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
