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

package bazel_test

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/docker/docker/client"
	"gvisor.dev/gvisor/pkg/sentry/state/checkpointfiles"
	"gvisor.dev/gvisor/pkg/test/dockerutil"
	"gvisor.dev/gvisor/test/benchmarks/harness"
)

// TestCheckpointWarmABSL is a single feasibility observation, not a paired
// benchmark. It checkpoints the persistent server from BenchmarkBuildABSL's
// workload and requires that same server to perform an incremental build with
// repository downloads disabled. Application pages and saved state are reported
// separately by the owning runtime.
func TestCheckpointWarmABSL(t *testing.T) {
	if rt := dockerutil.Runtime(); rt == "" || rt == "runc" {
		t.Fatal("warm checkpoint requires the qualified runsc runtime")
	}
	output, ok := os.LookupEnv("TEST_UNDECLARED_OUTPUTS_DIR")
	if !ok || !filepath.IsAbs(output) {
		t.Fatal("warm checkpoint requires an absolute evidence directory")
	}
	type commandRecord struct {
		Args         []string      `json:"args"`
		PID          int           `json:"pid,omitempty"`
		ParentPID    int           `json:"parent_pid,omitempty"`
		Output       string        `json:"output"`
		OutputBytes  int           `json:"output_bytes"`
		OutputSHA256 string        `json:"output_sha256"`
		Elapsed      time.Duration `json:"elapsed_ns"`
		Error        string        `json:"error,omitempty"`
	}
	type serverRecord struct {
		PID       int             `json:"pid"`
		ParentPID int             `json:"parent_pid"`
		State     string          `json:"state"`
		JarExists bool            `json:"jar_exists"`
		StartTime string          `json:"start_time"`
		Command   string          `json:"command"`
		Exe       string          `json:"exe"`
		PIDFile   int             `json:"pid_file"`
		ServerJar string          `json:"server_jar"`
		Threads   int             `json:"threads"`
		FDs       int             `json:"fds"`
		Mappings  int             `json:"mappings"`
		Details   json.RawMessage `json:"details"`
	}
	record := struct {
		Status       string           `json:"status"`
		Container    string           `json:"container"`
		Runtime      string           `json:"runtime"`
		Commands     []commandRecord  `json:"commands"`
		Servers      []serverRecord   `json:"servers"`
		Files        map[string]int64 `json:"checkpoint_files"`
		CheckpointNS time.Duration    `json:"checkpoint_ns"`
		RestoreNS    time.Duration    `json:"restore_ns"`
		Incremental  json.RawMessage  `json:"incremental"`
	}{Status: "FAIL", Runtime: dockerutil.Runtime()}
	defer func() {
		data, err := json.MarshalIndent(record, "", "  ")
		if err != nil {
			t.Error(err)
			return
		}
		if err := os.WriteFile(filepath.Join(output, "warm-checkpoint.json"), data, 0644); err != nil {
			t.Error(err)
		}
	}()
	ctx, cancel := context.WithTimeout(t.Context(), 15*time.Minute)
	defer cancel()
	machine, err := harness.GetMachine()
	if err != nil {
		t.Fatal(err)
	}
	defer machine.CleanUp()
	c := machine.GetContainer(ctx, t)
	defer func() {
		cleanup, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
		defer cancel()
		c.CleanUp(cleanup)
	}()
	const workdir = "/data/abseil-cpp"
	build := []string{"build", "--enable_bzlmod=false", "--nofetch", "--repository_disable_download", "-c", "opt", "--host_cxxopt=-std=c++14", "--cxxopt=-std=c++14", "//absl/base/..."}
	bazelCommand := func(args ...string) []string {
		return append([]string{"bash", "-c", "cd /data/abseil-cpp && exec bazel \"$@\"", "bazel"}, args...)
	}
	// Restore intentionally kills processes originating in Docker Exec. Start
	// the persistent server from PID 1, and reserve Exec for observation and
	// the later incremental client. The completion marker follows native exits;
	// signal.pause only keeps the container's root process alive afterward.
	warmCommands, err := json.Marshal([][]string{
		{"mkdir", "-p", "/data"},
		{"cp", "-r", "/abseil-cpp", "/data/."},
		bazelCommand("fetch", "--enable_bzlmod=false", "-c", "opt", "--host_cxxopt=-std=c++14", "--cxxopt=-std=c++14", "//absl/base/..."),
		bazelCommand(append(build, "--execution_log_json_file=/tmp/warm-spawns.json")...),
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Spawn(ctx, dockerutil.RunOpts{Image: "benchmarks/absl"}, "python3", "-c", `import hashlib, json, os, pathlib, signal, subprocess, sys, time
if os.getpid() != 1:
 raise ValueError('warm workload must start from PID 1')
rows = []
status = 1
try:
 for args in json.loads(sys.argv[1]):
  start = time.monotonic_ns()
  child = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
  output, _ = child.communicate()
  status = child.returncode
  row = {'args': args, 'pid': child.pid, 'parent_pid': os.getpid(),
         'output': output.decode(), 'output_bytes': len(output),
         'output_sha256': hashlib.sha256(output).hexdigest(),
         'elapsed_ns': time.monotonic_ns() - start}
  if status:
   row['error'] = 'native exit: ' + str(status)
  rows.append(row)
  if status:
   break
finally:
 pathlib.Path('/tmp/warm-commands.json').write_text(json.dumps(rows))
 print('WARM_ABSL_EXIT=' + str(status), flush=True)
signal.pause()`, string(warmCommands)); err != nil {
		t.Fatal(err)
	}
	record.Container = c.ID()
	completion, err := c.WaitForOutputSubmatch(ctx, `WARM_ABSL_EXIT=(-?[0-9]+)`, 10*time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	exec := func(args ...string) string {
		t.Helper()
		start := time.Now()
		out, err := c.Exec(ctx, dockerutil.ExecOpts{WorkDir: "/"}, args...)
		row := commandRecord{Args: args, Output: out, Elapsed: time.Since(start), OutputBytes: len(out), OutputSHA256: fmt.Sprintf("%x", sha256.Sum256([]byte(out)))}
		if err != nil {
			row.Error = err.Error()
		}
		record.Commands = append(record.Commands, row)
		if err != nil {
			t.Fatalf("command %v: %v\n%s", args, err, out)
		}
		return out
	}
	var warm []commandRecord
	if err := json.Unmarshal([]byte(exec("cat", "/tmp/warm-commands.json")), &warm); err != nil {
		t.Fatal(err)
	}
	record.Commands[len(record.Commands)-1].Output = ""
	record.Commands = append(record.Commands, warm...)
	if len(completion) != 2 || completion[1] != "0" || len(warm) != 4 {
		t.Fatalf("warm PID 1 workload did not complete: %v (%d commands)", completion, len(warm))
	}
	for _, command := range warm {
		if command.Error != "" || command.PID <= 1 || command.ParentPID != 1 {
			t.Fatalf("warm command did not originate from PID 1 and succeed: %+v", command)
		}
	}
	if !strings.Contains(warm[3].Output, "Build completed successfully") {
		t.Fatalf("warm build did not report completion: %s", warm[3].Output)
	}
	if got := strings.TrimSpace(exec("git", "-C", workdir, "rev-parse", "HEAD")); got != "79e414672f447ac1b27f8cb27ad61df315a3792b" {
		t.Fatalf("ABSL source = %q", got)
	}
	if got := strings.TrimSpace(exec("git", "-C", workdir, "status", "--porcelain", "--untracked-files=no")); got != "" {
		t.Fatalf("ABSL tracked source changed: %s", got)
	}
	if got := strings.Fields(exec("sha256sum", workdir+"/WORKSPACE")); len(got) != 2 || got[0] != "a3be2fd68c7789858e5bde9dd95c6d43210eebeb55fdce57a26d37a164ec6a12" {
		t.Fatalf("ABSL dependency graph differs: %v", got)
	}
	if got := strings.TrimSpace(exec("bazel", "--version")); got != "bazel 7.3.2" {
		t.Fatalf("Bazel version = %q", got)
	}
	bazel := func(args ...string) string {
		t.Helper()
		return exec(bazelCommand(args...)...)
	}
	// The fixed WORKSPACE was fetched with the same opt/C++ configuration.
	// Both builds prohibit repository downloads; this does not isolate arbitrary
	// network access by repository commands.
	serverPID := func() int {
		t.Helper()
		pidText := bazel("info", "--enable_bzlmod=false", "--nofetch", "--repository_disable_download", "server_pid")
		var pid int
		for _, line := range strings.Split(pidText, "\n") {
			if n, err := strconv.Atoi(strings.TrimSpace(line)); err == nil {
				if pid != 0 || n <= 1 {
					t.Fatalf("ambiguous server PID: %q", pidText)
				}
				pid = n
			}
		}
		if pid == 0 {
			t.Fatalf("server PID missing: %q", pidText)
		}
		return pid
	}
	pid := serverPID()
	var outputBase string
	for _, line := range strings.Split(bazel("info", "--enable_bzlmod=false", "--nofetch", "--repository_disable_download", "output_base"), "\n") {
		if filepath.IsAbs(line) {
			if outputBase != "" {
				t.Fatal("ambiguous Bazel output base")
			}
			outputBase = line
		}
	}
	if outputBase == "" {
		t.Fatal("Bazel output base missing")
	}
	// Read the process before invoking any Bazel client after restore, so a
	// client cannot silently start a replacement server and satisfy the check.
	snapshot := func() serverRecord {
		t.Helper()
		out := exec("python3", "-c", `import json, os, pathlib, sys
p = pathlib.Path('/proc') / sys.argv[1]
stat = (p / 'stat').read_text().rsplit(')', 1)[1].split()
args = (p / 'cmdline').read_bytes().decode().rstrip('\0').split('\0')
jar_indices = [i for i, arg in enumerate(args) if arg == '-jar']
jar = args[jar_indices[0] + 1] if len(jar_indices) == 1 and jar_indices[0] + 1 < len(args) else ''
pid_file = int((pathlib.Path(sys.argv[2]) / 'server/server.pid.txt').read_text())
details = {'status': (p / 'status').read_text(), 'maps': (p / 'maps').read_text(),
           'fd_targets': {}, 'vanished_fds': [],
           'task_ids': sorted(x.name for x in (p / 'task').iterdir()),
           'init_cmdline': pathlib.Path('/proc/1/cmdline').read_bytes().decode(),
           'init_stat': pathlib.Path('/proc/1/stat').read_text()}
for fd in sorted((p / 'fd').iterdir()):
 try:
  details['fd_targets'][fd.name] = os.readlink(fd)
 except FileNotFoundError:
  details['vanished_fds'].append(fd.name)
print(json.dumps({'pid': int(sys.argv[1]), 'start_time': stat[19],
 'command': '\0'.join(args), 'exe': os.readlink(p / 'exe') if (p / 'exe').exists() else '',
 'parent_pid': int(stat[1]), 'state': stat[0],
 'pid_file': pid_file, 'server_jar': jar, 'jar_exists': bool(jar) and pathlib.Path(jar).is_file(),
 'threads': len(details['task_ids']), 'fds': len(details['fd_targets']),
 'mappings': len(details['maps'].splitlines()), 'details': details}))`, strconv.Itoa(pid), outputBase)
		var server serverRecord
		if err := json.Unmarshal([]byte(out), &server); err != nil {
			t.Fatal(err)
		}
		// The decoded record below owns the large process snapshot. Retain
		// only its byte identity in the command history.
		record.Commands[len(record.Commands)-1].Output = ""
		record.Servers = append(record.Servers, server)
		if server.PID != pid || server.PIDFile != pid || server.ParentPID != 1 || server.State == "Z" || server.Threads < 2 || server.FDs == 0 || !filepath.IsAbs(server.ServerJar) || !server.JarExists {
			t.Fatalf("not a live Bazel server: %+v", server)
		}
		return server
	}
	before := snapshot()
	cli, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
	if err != nil {
		t.Fatal(err)
	}
	defer cli.Close()
	info, err := cli.Info(ctx)
	if err != nil {
		t.Fatal(err)
	}
	const checkpoint = "warm-absl"
	start := time.Now()
	if err := c.Checkpoint(ctx, checkpoint); err != nil {
		t.Fatal(err)
	}
	if err := c.WaitTimeout(ctx, 30*time.Second); err != nil {
		t.Fatal(err)
	}
	record.CheckpointNS = time.Since(start)
	directory := filepath.Join(info.DockerRootDir, "containers", c.ID(), "checkpoints", checkpoint)
	entries, err := os.ReadDir(directory)
	if err != nil {
		t.Fatal(err)
	}
	record.Files = make(map[string]int64)
	for _, entry := range entries {
		file, err := entry.Info()
		if err != nil || !file.Mode().IsRegular() {
			t.Fatalf("checkpoint entry %s is not a regular file: %v", entry.Name(), err)
		}
		record.Files[entry.Name()] = file.Size()
	}
	if record.Files[checkpointfiles.StateFileName] <= 0 || record.Files[checkpointfiles.PagesFileName] <= 0 {
		t.Fatalf("checkpoint files are incomplete: %v", record.Files)
	}
	start = time.Now()
	if err := c.Restore(ctx, checkpoint); err != nil {
		t.Fatal(err)
	}
	record.RestoreNS = time.Since(start)
	after := snapshot()
	if before.PID != after.PID || before.StartTime != after.StartTime || before.Command != after.Command || before.Exe != after.Exe || before.ServerJar != after.ServerJar {
		t.Fatalf("server identity changed across restore: before=%+v after=%+v", before, after)
	}
	if out := bazel(append(build, "--execution_log_json_file=/tmp/incremental-spawns.json")...); !strings.Contains(out, "Build completed successfully") {
		t.Fatalf("incremental build did not report completion: %s", out)
	}
	if got := serverPID(); got != pid {
		t.Fatalf("incremental client used server %d, want checkpointed server %d", got, pid)
	}
	final := snapshot()
	if before.PID != final.PID || before.StartTime != final.StartTime || before.Command != final.Command || before.Exe != final.Exe || before.ServerJar != final.ServerJar {
		t.Fatal("incremental build replaced the checkpointed server")
	}
	// An action cache hit must not merely hide a server restart. In addition to
	// the process identity above, retain actual spawn records and require the
	// incremental build to execute no compiler or other subprocess action.
	spawns := exec("python3", "-c", `import hashlib, json, pathlib
def records(name):
 b = pathlib.Path(name).read_bytes()
 text = b.decode(); rows = []; decoder = json.JSONDecoder()
 while text.strip():
  row, end = decoder.raw_decode(text.lstrip()); rows.append(row)
  text = text.lstrip()[end:]
 return {'bytes': len(b), 'sha256': hashlib.sha256(b).hexdigest(), 'spawns': rows}
print(json.dumps({'warm': records('/tmp/warm-spawns.json'), 'incremental': records('/tmp/incremental-spawns.json')}))`)
	var counts struct {
		Warm struct {
			Spawns []json.RawMessage `json:"spawns"`
		} `json:"warm"`
		Incremental struct {
			Spawns []json.RawMessage `json:"spawns"`
		} `json:"incremental"`
	}
	if err := json.Unmarshal([]byte(spawns), &counts); err != nil {
		t.Fatal(err)
	}
	record.Incremental = json.RawMessage(spawns)
	record.Commands[len(record.Commands)-1].Output = ""
	if len(counts.Warm.Spawns) == 0 || len(counts.Incremental.Spawns) != 0 {
		t.Fatalf("warm/incremental spawns = %d/%d, want positive/zero", len(counts.Warm.Spawns), len(counts.Incremental.Spawns))
	}
	record.Status = "PASS"
}
