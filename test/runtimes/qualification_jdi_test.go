package qualification_test

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

const image = "us-central1-docker.pkg.dev/gvisor-presubmit/gvisor-presubmit-images/runtimes/java21_x86_64@sha256:c5a1419701eea132bc82193018a09a91296a8d9e96492495a72dd84040e0bb47"
const jdiTest = "com/sun/jdi/ThreadMemoryLeakTest.java"

var proctor = flag.String("proctor", "", "runfile of the CI test proctor")

func docker(t *testing.T, args ...string) string {
	t.Helper()
	out, err := exec.CommandContext(t.Context(), "docker", args...).CombinedOutput()
	if err != nil {
		t.Fatalf("docker %s: %v\n%s", args[0], err, out)
	}
	return strings.TrimSpace(string(out))
}

func TestJDI(t *testing.T) {
	bin, err := filepath.EvalSymlinks(filepath.Join(os.Getenv("TEST_SRCDIR"), os.Getenv("TEST_WORKSPACE"), *proctor))
	if err != nil {
		t.Fatal(err)
	}
	oopsFlag := regexp.MustCompile(`(?m)UseCompressedOops\s+(?::?=)\s+(true|false)`)
	heapFlag := regexp.MustCompile(`(?m)MaxHeapSize\s+(?::?=)\s+(\d+)`)
	bye := regexp.MustCompile(`Goodbye from ThreadMemoryLeakTarg after ([0-9]+) iterations`)
	for i, runtime := range []string{"runc", "java-jdi-ci"} {
		t.Run(fmt.Sprintf("%d-%s", i, runtime), func(t *testing.T) {
			name := fmt.Sprintf("jdi-%d-%s", i, runtime)
			docker(t, "run", "-d", "--name", name, "--runtime", runtime, "--network=none", "-v", bin+":/proctor:ro", image, "/proctor", "--pause")
			t.Cleanup(func() {
				if out, err := exec.Command("docker", "rm", "-f", name).CombinedOutput(); err != nil {
					t.Errorf("docker rm: %v\n%s", err, out)
				}
			})
			if got := docker(t, "inspect", "-f", "{{.HostConfig.Runtime}}", name); got != runtime {
				t.Fatalf("runtime %q, want %q", got, runtime)
			}
			flags := docker(t, "exec", name, "java", "-Xmx7m", "-XX:+PrintFlagsFinal", "-version")
			oops, heap := oopsFlag.FindStringSubmatch(flags), heapFlag.FindStringSubmatch(flags)
			if len(oops) != 2 || len(heap) != 2 {
				t.Fatal("could not read the JVM flags")
			}
			cmd := exec.CommandContext(t.Context(), "docker", "exec", "-u", "0", name, "/proctor", "--runtime=java", "--tests="+jdiTest, "--timeout=15m")
			var stdout, stderr bytes.Buffer
			cmd.Stdout = &stdout
			cmd.Stderr = &stderr
			cohort := map[string]any{}
			peers := startCohort(t, name, bin, cohort)
			if err := cmd.Start(); err != nil {
				t.Fatal(err)
			}
			done := make(chan struct{})
			var runErr error
			go func() { runErr = cmd.Wait(); close(done) }()
			measureCohort(t, name, peers, done, cohort)
			<-done
			outDir := os.Getenv("TEST_UNDECLARED_OUTPUTS_DIR")
			write := func(ext string, data []byte) {
				t.Helper()
				if err := os.WriteFile(filepath.Join(outDir, name+ext), data, 0644); err != nil {
					t.Fatal(err)
				}
			}
			write(".stdout", stdout.Bytes())
			write(".stderr", stderr.Bytes())
			if cmd.ProcessState == nil {
				t.Fatalf("could not start proctor: %v", runErr)
			}
			record := map[string]any{"cohort": cohort, "runtime": runtime, "jdk_test": jdiTest, "compressed_oops": oops[1], "max_heap_bytes": heap[1], "proctor_exit_code": cmd.ProcessState.ExitCode()}
			writeRecord := func() {
				t.Helper()
				result, err := json.MarshalIndent(record, "", "  ")
				if err != nil {
					t.Fatal(err)
				}
				write(".json", result)
			}
			writeRecord()
			// Read the result in the same filesystem view as jtreg.
			path := "/root/JTwork/" + strings.TrimSuffix(jdiTest, ".java") + ".jtr"
			cat := exec.CommandContext(t.Context(), "docker", "exec", "-u", "0", name, "cat", path)
			data, err := cat.Output()
			if err != nil {
				ls, _ := exec.CommandContext(t.Context(), "docker", "exec", "-u", "0", name, "ls", "-ld", "/root/JTwork", filepath.Dir(path), path).CombinedOutput()
				t.Fatalf("reading guest JTR: %v; guest ls: %s", err, ls)
			}
			write(".jtr", data)
			body := string(data)
			passed := strings.Contains(body, "execStatus=Passed.") && strings.Contains(body, "ThreadMemoryLeakTest: PASSED") && strings.Contains(body, "test result: Passed.")
			timeout := regexp.MustCompile(`(?m)^test result: Error\. .*timed out \(timeout set to 600000ms,`).MatchString(body)
			debuggerOOM := strings.Contains(body, `java.lang.OutOfMemoryError thrown from the UncaughtExceptionHandler in thread "JDI Target VM Interface"`)
			record["passed"], record["jtreg_timeout"], record["debugger_oom"] = passed, timeout, debuggerOOM
			if m := bye.FindStringSubmatch(body); m != nil {
				record["target_iterations"] = m[1]
			}
			writeRecord()
			if (runErr == nil) != passed || (!passed && !(timeout && debuggerOOM)) {
				t.Errorf("unexpected proctor exit %d; passed=%v timeout=%v debugger OOM=%v", cmd.ProcessState.ExitCode(), passed, timeout, debuggerOOM)
			}
		})
	}
}
