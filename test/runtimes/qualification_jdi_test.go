package qualification_test

import (
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
	for i, runtime := range []string{"runc", "java-jdi-ci", "java-jdi-ci", "runc"} {
		t.Run(fmt.Sprintf("%d-%s", i+1, runtime), func(t *testing.T) {
			name := fmt.Sprintf("jdi-%d-%s", i+1, runtime)
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
			output, runErr := cmd.CombinedOutput()
			outDir := os.Getenv("TEST_UNDECLARED_OUTPUTS_DIR")
			jtr := filepath.Join(outDir, name+".jtr")
			docker(t, "cp", name+":/root/JTwork/"+strings.TrimSuffix(jdiTest, ".java")+".jtr", jtr)
			data, err := os.ReadFile(jtr)
			if err != nil {
				t.Fatal(err)
			}
			body := string(data)
			passed := strings.Contains(body, "execStatus=Passed.") && strings.Contains(body, "ThreadMemoryLeakTest: PASSED")
			timeout := strings.Contains(body, "timed out") && strings.Contains(body, "600000ms")
			debuggerOOM := strings.Contains(body, "OutOfMemoryError") && strings.Contains(body, "JDI Target VM Interface")
			record := map[string]any{"runtime": runtime, "jdk_test": jdiTest, "compressed_oops": oops[1], "max_heap_bytes": heap[1], "passed": passed, "jtreg_timeout": timeout, "debugger_oom": debuggerOOM}
			if m := bye.FindStringSubmatch(body); m != nil {
				record["target_iterations"] = m[1]
			}
			result, err := json.MarshalIndent(record, "", "  ")
			if err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(outDir, name+".json"), result, 0644); err != nil {
				t.Fatal(err)
			}
			t.Logf("%s", result)
			if (runErr == nil) != passed || (!passed && !(timeout && debuggerOOM)) {
				t.Errorf("unexpected proctor outcome: %v (%d output bytes), see %s", runErr, len(output), filepath.Base(jtr))
			}
		})
	}
}
