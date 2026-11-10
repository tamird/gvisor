package qualification_test

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

type cohortPeer struct {
	name   string
	tests  []string
	done   chan struct{}
	cancel context.CancelFunc
	err    error
}

// Each peer gets its own work directory and jtreg agent pool, as in CI.
func startCohort(t *testing.T, prefix, bin string, ctl map[string]any) []*cohortPeer {
	t.Helper()
	source := filepath.Join(os.Getenv("TEST_SRCDIR"), os.Getenv("TEST_WORKSPACE"), "test/runtimes/qualification_jdi_cases.json")
	data, err := os.ReadFile(source)
	if err != nil {
		t.Fatal(err)
	}
	var batches [][]string
	if err := json.Unmarshal(data, &batches); err != nil || len(batches) != 8 || len(batches[3]) != 21 || batches[3][0] != jdiTest {
		t.Fatalf("invalid source cohort: %v", err)
	}
	memory, err := strconv.ParseInt(docker(t, "info", "-f", "{{.MemTotal}}"), 10, 64)
	if err != nil {
		t.Fatal(err)
	}
	ctl["worker_memory_bytes"], ctl["worker_cpus"] = memory, docker(t, "info", "-f", "{{.NCPU}}")
	for _, name := range []string{"cpu.max", "cpuset.cpus.effective", "memory.max"} {
		if b, err := os.ReadFile(filepath.Join("/sys/fs/cgroup", name)); err == nil {
			ctl[name] = strings.TrimSpace(string(b))
			if value, err := strconv.ParseInt(strings.TrimSpace(string(b)), 10, 64); name == "memory.max" && err == nil && value < memory {
				memory = value
			}
		}
	}
	// Reserve at least four GiB for Bazel, Docker and the measured JVMs.
	limit := min(int64(1280<<20), (memory-(4<<30))/7)
	if limit < 384<<20 {
		t.Fatalf("worker has too little spare memory for seven JVM containers: %d", memory)
	}
	ctl["peer_memory_limit_bytes"] = limit
	var peers []*cohortPeer
	for i, batch := range batches {
		if i == 3 {
			continue
		}
		p := &cohortPeer{name: fmt.Sprintf("%s-peer-%d", prefix, i+1), tests: batch, done: make(chan struct{})}
		docker(t, "run", "-d", "--name", p.name, "--runtime", "java-jdi-ci", "--network=none", "--memory", strconv.FormatInt(limit, 10), "-v", bin+":/proctor:ro", image, "/proctor", "--pause")
		t.Cleanup(func() {
			// A peer still running after the bounded interval is intentionally stopped.
			ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
			defer cancel()
			if out, err := exec.CommandContext(ctx, "docker", "rm", "-f", p.name).CombinedOutput(); err != nil && !strings.Contains(string(out), "No such container") {
				t.Errorf("removing peer %s: %v (%s)", p.name, err, out)
			}
		})
		peers = append(peers, p)
	}
	names := []string{prefix}
	for _, p := range peers {
		names = append(names, p.name)
	}
	ctl["cgroups_before"] = cgroupCounters(t, names)
	for _, p := range peers {
		ctx, cancel := context.WithCancel(t.Context())
		p.cancel = cancel
		t.Cleanup(cancel)
		cmd := exec.CommandContext(ctx, "docker", "exec", "-u", "0", p.name, "/proctor", "--runtime=java", "--timeout=8m", "--tests="+strings.Join(p.tests, ","))
		if err := cmd.Start(); err != nil {
			t.Fatal(err)
		}
		go func() { p.err = cmd.Wait(); close(p.done) }()
	}
	return peers
}

// Read counters only from the cgroup that Docker assigned to this exact
// container. A sandbox whose exported host PID lives outside it still has
// Docker stats observations, but cannot supply these additional counters.
func cgroupCounters(t *testing.T, names []string) map[string]map[string]string {
	t.Helper()
	result := make(map[string]map[string]string)
	ctx, cancel := context.WithTimeout(t.Context(), 4*time.Second)
	defer cancel()
	data, err := exec.CommandContext(ctx, "docker", append([]string{"inspect", "-f", "{{.Name}} {{.State.Pid}} {{.Id}}"}, names...)...).Output()
	if err != nil {
		return result
	}
	out := strings.TrimSpace(string(data))
	for _, line := range strings.Split(out, "\n") {
		words := strings.Fields(line)
		if len(words) != 3 {
			continue
		}
		data, err := os.ReadFile(filepath.Join("/proc", words[1], "cgroup"))
		if err != nil {
			continue
		}
		dir, found := strings.CutPrefix(strings.TrimSpace(string(data)), "0::/")
		if !found {
			continue
		}
		for dir != "." && dir != "/" {
			if !strings.Contains(filepath.Base(dir), words[2]) {
				dir = filepath.Dir(dir)
				continue
			}
			values := make(map[string]string)
			for _, stat := range []string{"cpu.stat", "memory.current", "memory.peak", "memory.events"} {
				if b, err := os.ReadFile(filepath.Join("/sys/fs/cgroup", dir, stat)); err == nil {
					values[stat] = strings.TrimSpace(string(b))
				}
			}
			result[strings.TrimPrefix(words[0], "/")] = values
			break
		}
	}
	return result
}

func measureCohort(t *testing.T, name string, peers []*cohortPeer, victimDone <-chan struct{}, result map[string]any) {
	t.Helper()
	start := time.Now()
	ctx, cancel := context.WithTimeout(t.Context(), 110*time.Second)
	defer cancel()
	var samples []map[string]any
	var names []string
	for _, p := range peers {
		names = append(names, p.name)
	}
loop:
	for {
		active := 0
		for _, p := range peers {
			select {
			case <-p.done:
			default:
				active++
			}
		}
		// Docker reads the cgroup counters for each named container. Keep the
		// original per-container samples and time alongside process liveness.
		args := []string{"stats", "--no-stream", "--format", "{{.Name}} {{.CPUPerc}} {{.MemUsage}} pids={{.PIDs}}", name}
		out, err := exec.CommandContext(ctx, "docker", append(args, names...)...).Output()
		if ctx.Err() != nil {
			break
		}
		if err != nil {
			result["sampling_error"] = err.Error()
			break
		}
		samples = append(samples, map[string]any{"seconds": time.Since(start).Seconds(), "active_peer_proctors": active, "docker_stats": strings.TrimSpace(string(out))})
		select {
		case <-victimDone:
			break loop
		case <-ctx.Done():
			break loop
		case <-time.After(4 * time.Second):
		}
	}
	result["peer_interval_seconds"] = time.Since(start).Seconds()
	result["samples"] = samples
	result["exposure_observed"] = len(samples) > 0
	ctl := map[string]any{}
	for _, p := range peers {
		select {
		case <-p.done:
			status := "finished"
			if p.err != nil {
				status = p.err.Error()
			}
			ctl[p.name] = status
		default:
			ctl[p.name] = "still running at cutoff"
		}
	}
	result["cgroups_at_cutoff"] = cgroupCounters(t, append([]string{name}, names...))
	stopCtx, stop := context.WithTimeout(t.Context(), 12*time.Second)
	kill := exec.CommandContext(stopCtx, "docker", append([]string{"kill"}, names...)...)
	out, err := kill.CombinedOutput()
	stop()
	if err != nil {
		result["kill_error"] = fmt.Sprintf("%v: %s", err, strings.TrimSpace(string(out)))
	}
	waitCtx, endWait := context.WithTimeout(t.Context(), 4*time.Second)
	defer endWait()
	for _, p := range peers {
		select {
		case <-p.done:
		case <-waitCtx.Done():
			p.cancel()
			ctl[p.name] = fmt.Sprintf("%v; exec did not exit after kill", ctl[p.name])
		}
	}
	// A Docker error, including OOM during the experiment, must not discard
	// the DUT's JTR. Retain the live status and remove any surviving peers.
	inspectCtx, endInspect := context.WithTimeout(t.Context(), 4*time.Second)
	info, inspectErr := exec.CommandContext(inspectCtx, "docker", append([]string{"inspect", "-f", "{{.Name}} {{.State.Running}} {{.State.OOMKilled}}"}, names...)...).Output()
	endInspect()
	result["container_states"] = strings.TrimSpace(string(info))
	if inspectErr != nil {
		result["inspect_error"] = inspectErr.Error()
	}
	removeCtx, endRemove := context.WithTimeout(t.Context(), 12*time.Second)
	if out, err := exec.CommandContext(removeCtx, "docker", append([]string{"rm", "-f"}, names...)...).CombinedOutput(); err != nil {
		result["remove_error"] = fmt.Sprintf("%v: %s", err, strings.TrimSpace(string(out)))
	}
	endRemove()
	for _, p := range peers {
		ctl[p.name] = map[string]any{"at_cutoff": ctl[p.name], "first_test": p.tests[0], "offered_cases": len(p.tests)}
	}
	result["peers"] = ctl
}
