package qualification_test

import (
	"bufio"
	"encoding/json"
	"flag"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"gvisor.dev/gvisor/test/runtimes/runner/lib"
)

const image = "us-central1-docker.pkg.dev/gvisor-presubmit/gvisor-presubmit-images/runtimes/java21_x86_64@sha256:c5a1419701eea132bc82193018a09a91296a8d9e96492495a72dd84040e0bb47"
const unref = "java/nio/channels/DatagramChannel/Unref.java"

var proctor = flag.String("proctor", "", "runfile path to the built proctor")

func docker(t *testing.T, args ...string) string {
	t.Helper()
	cmd := exec.Command("docker", args...)
	cmd.Stderr = os.Stderr
	out, err := cmd.Output()
	if err != nil {
		t.Fatalf("docker %s: %v\n%s", args[0], err, out)
	}
	return string(out)
}

func TestJavaAdmission(t *testing.T) {
	root := filepath.Join(os.Getenv("TEST_SRCDIR"), os.Getenv("TEST_WORKSPACE"))
	bin, err := filepath.EvalSymlinks(filepath.Join(root, *proctor))
	if err != nil {
		t.Fatal(err)
	}
	before, err := lib.ExcludeFilter(filepath.Join(root, "test/runtimes/java21-parent.csv"))
	if err != nil {
		t.Fatal(err)
	}
	after, err := lib.ExcludeFilter(filepath.Join(root, "test/runtimes/exclude/java21.csv"))
	if err != nil {
		t.Fatal(err)
	}
	inventory := strings.Fields(docker(t, "run", "--rm", "--network=none", "-v", bin+":/proctor:ro", image, "/proctor", "--runtime=java", "--list"))
	problems := docker(t, "run", "--rm", "--network=none", image, "cat", "/root/jdk/test/jdk/ProblemList.txt")
	linuxProblems := make(map[string]bool)
	for scanner := bufio.NewScanner(strings.NewReader(problems)); scanner.Scan(); {
		fields := strings.Fields(strings.SplitN(scanner.Text(), "#", 2)[0])
		if len(fields) != 3 {
			continue
		}
		for target := range strings.SplitSeq(fields[2], ",") {
			if target == "generic-all" || target == "linux-all" || target == "linux-x64" {
				linuxProblems[fields[0]] = true
			}
		}
	}
	var removed, collateral []string
	oldCount, newCount := 0, 0
	for _, name := range inventory {
		if before(name) {
			oldCount++
		}
		if after(name) {
			newCount++
			if !before(name) {
				t.Fatalf("unexpected admission %s", name)
			}
			if linuxProblems[name] {
				collateral = append(collateral, name)
			}
		} else if before(name) {
			removed = append(removed, name)
		}
	}
	if !slices.Equal(removed, []string{unref}) || oldCount != newCount+1 || !linuxProblems[unref] {
		t.Fatalf("old=%d new=%d removed=%v", oldCount, newCount, removed)
	}
	outDir := os.Getenv("TEST_UNDECLARED_OUTPUTS_DIR")
	result, err := json.MarshalIndent(map[string]any{"image": image, "listed": len(inventory), "old": oldCount, "new": newCount, "removed": removed, "other_admitted_linux_problem_entries": collateral}, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(outDir, "admission.json"), result, 0644); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"java/nio/channels/DatagramChannel/Connect.java", "java/lang/management/MemoryMXBean/Pending.java", "java/util/concurrent/forkjoin/AsyncShutdownNow.java"} {
		if !slices.Contains(inventory, name) || !after(name) {
			t.Fatalf("expected admitted control %s", name)
		}
		if !strings.Contains(name, "/DatagramChannel/") && !slices.Contains(collateral, name) {
			t.Fatalf("control missing from supplier problem list: %s", name)
		}
		t.Run(filepath.Base(name), func(t *testing.T) {
			work, err := os.MkdirTemp(os.Getenv("JAVA_UNREF"), "jtwork-")
			if err != nil {
				t.Fatal(err)
			}
			if err := os.Chmod(work, 0777); err != nil {
				t.Fatal(err)
			}
			id := strings.TrimSpace(docker(t, "create", "--network=none", "--runtime=java-unref-ci", "-v", work+":/jtwork", image, "jtreg", "-agentvm", "-dir:/root/jdk/test/jdk", "-w:/jtwork", "-noreport", "-timeoutFactor:2", "-tl:200", "-verbose:summary", name))
			t.Cleanup(func() { docker(t, "rm", "-f", id) })
			start := exec.Command("docker", "start", "--attach", id)
			output, startErr := start.CombinedOutput()
			state := strings.TrimSpace(docker(t, "inspect", "--format", "{{.HostConfig.Runtime}} {{.State.ExitCode}} {{.State.OOMKilled}}", id))
			file := strings.TrimSuffix(name, ".java") + ".jtr"
			dst := filepath.Join(outDir, filepath.Base(file))
			body, err := os.ReadFile(filepath.Join(work, file))
			if err == nil {
				if err := os.WriteFile(dst, body, 0644); err != nil {
					t.Fatal(err)
				}
			}
			if err != nil || startErr != nil || state != "java-unref-ci 0 false" || !strings.Contains(string(body), "execStatus=Passed.") {
				t.Fatalf("%s: state=%s start=%v read=%v\n%s", name, state, startErr, err, output)
			}
		})
	}
}
