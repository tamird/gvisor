// Fork-only measurement of the external reporter’s two real TCP stacks.
package handshakefloor

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

var program = flag.String("reporter", "", "runfile path to the pinned external reporter")

// Each subprocess runs one new connection through the reporter's two real
// stacks, with its original two warm-up fetches and one measured fetch.
func TestEndOfTransfer(t *testing.T) {
	exe := filepath.Join(os.Getenv("TEST_SRCDIR"), os.Getenv("TEST_WORKSPACE"), *program)
	if *program == "" {
		t.Fatal("missing reporter")
	}
	for seed := 1; seed <= 4; seed++ {
		controllers := []string{"reno", "cubic"}
		if seed%2 == 0 {
			slices.Reverse(controllers)
		}
		for _, controller := range controllers {
			for _, timestamps := range []bool{seed%2 == 0, seed%2 != 0} {
				policies := []string{"data", "pr", "precise"}
				if !timestamps {
					// Without TS, #16381 already uses the precise local clock.
					policies = policies[:2]
				}
				if seed%2 == 0 {
					slices.Reverse(policies)
				} else if seed == 3 && timestamps {
					policies = []string{"pr", "precise", "data"}
				}
				for _, policy := range policies {
					t.Run(fmt.Sprintf("%s/ts%t/seed%d/%s", controller, timestamps, seed, policy), func(t *testing.T) {
						ctx, cancel := context.WithTimeout(context.Background(), 55*time.Second)
						defer cancel()
						cmd := exec.CommandContext(ctx, exe, "-cc", controller, "-size", "14400", "-burst-keep-first", "8", "-burst-drop", "4", "-burst-window", "0", "-trials", "1", "-seed", strconv.Itoa(seed), "-client-timestamps="+strconv.FormatBool(timestamps), "-trace", "-timeout", "25s")
						cmd.Env = append(os.Environ(), "RACK_STUDY_POLICY="+policy, "GOMAXPROCS=2")
						out, err := runOnTwoCPUs(t, cmd)
						log := string(out)
						t.Log(log)
						if dir := os.Getenv("TEST_UNDECLARED_OUTPUTS_DIR"); dir != "" {
							name := fmt.Sprintf("%s-ts%t-seed%d-%s.log", controller, timestamps, seed, policy)
							if err := os.WriteFile(filepath.Join(dir, name), out, 0644); err != nil {
								t.Fatal(err)
							}
						}
						for _, line := range strings.Split(log, "\n") {
							if !strings.HasPrefix(line, "PROBE ") {
								continue
							}
							var start, end, ack uint32
							var measured, actual, data, pr, precise int64
							var accept bool
							if n, err := fmt.Sscanf(line, "PROBE seq=%d end=%d ack=%d measured=%d actual=%d data=%d pr=%d precise=%d accept=%t", &start, &end, &ack, &measured, &actual, &data, &pr, &precise, &accept); n != 9 || err != nil {
								t.Errorf("invalid probe: %s (%v)", line, err)
							}
							want := map[string]int64{"data": data, "pr": pr, "precise": precise}[policy]
							if int32(start-ack) <= 0 || int32(end-start) <= 0 || measured <= 0 || actual != want || accept != (measured >= actual) {
								t.Errorf("inconsistent observed probe %q, want active floor %d", line, want)
							}
						}
						if err != nil {
							t.Fatalf("reporter: %v", err)
						}
						tsState := "off"
						if timestamps {
							tsState = "on"
						}
						for _, needle := range []string{"HS policy=" + policy + " ", "retried=false ts=" + strconv.FormatBool(timestamps) + " ", "connection: TCP timestamps " + tsState + ")", "trial 1 fetch 1 warm-up", "trial 1 fetch 2 warm-up", "trial 1 fetch 3 measured", "measured fetches: 1,", "path drops toward the client: 0 by the queue, 4 scripted"} {
							if !strings.Contains(log, needle) {
								t.Errorf("missing original report %q", needle)
							}
						}
						if strings.Contains(log, "ERROR:") || strings.Contains(log, "TIMED OUT") {
							t.Error("a fetch did not complete")
						}
						// The probe predicate and transfer RTOs are observations, not
						// required outcomes. A case with no SACKed retransmitted probe
						// must not be classified as a rejection.
					})
				}
			}
		}
	}
}

func runOnTwoCPUs(t *testing.T, cmd *exec.Cmd) ([]byte, error) {
	t.Helper()
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	var available, two unix.CPUSet
	if err := unix.SchedGetaffinity(0, &available); err != nil {
		t.Fatal(err)
	}
	for cpu := 0; cpu < len(available)*64 && two.Count() < 2; cpu++ {
		if available.IsSet(cpu) {
			two.Set(cpu)
		}
	}
	if two.Count() != 2 {
		t.Fatalf("available CPU set only provided %d CPUs", two.Count())
	}
	if err := unix.SchedSetaffinity(0, &two); err != nil {
		t.Fatal(err)
	}
	defer func() {
		if err := unix.SchedSetaffinity(0, &available); err != nil {
			t.Errorf("restore CPU affinity: %v", err)
		}
	}()
	return cmd.CombinedOutput()
}
