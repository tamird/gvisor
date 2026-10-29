package main

import (
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"testing"
)

func TestMain(m *testing.M) {
	if _, replay := os.LookupEnv("GVISOR_REPLAY_CHILD"); replay {
		main()
		return
	}
	os.Exit(m.Run())
}

// Run the reporter's long-lived scenario: one client, two warm-up transfers,
// then five measured transfers subject to a burst of packet loss.
func TestLongLived(t *testing.T) {
	cmd := exec.Command(os.Args[0], "-trials=1", "-fetches=5")
	cmd.Env = append(os.Environ(), "GVISOR_REPLAY_CHILD=1")
	output, err := cmd.CombinedOutput()
	t.Logf("%s", output)
	if err != nil {
		t.Fatal(err)
	}
	raw := string(output)
	for _, marker := range []string{"RACK on, cubic, host GSO true", "(gVisor connection: TCP timestamps on)", "measured fetches: 5,"} {
		if !strings.Contains(raw, marker) {
			t.Fatalf("missing %q", marker)
		}
	}
	rows := regexp.MustCompile(`(?m)^trial 1 fetch ([0-9]+) (warm-up|measured) +([0-9.]+) s [^\n]*$`).FindAllStringSubmatch(raw, -1)
	if got, want := len(rows), 7; got != want {
		t.Fatalf("completed fetch records = %d, want %d", got, want)
	}
	for i, row := range rows {
		want := "measured"
		if i < 2 {
			want = "warm-up"
		}
		if row[1] != strconv.Itoa(i+1) || row[2] != want || strings.Contains(row[0], "ERROR:") || strings.Contains(row[0], "TIMED OUT") {
			t.Fatalf("fetch %d: %s", i+1, row[0])
		}
	}
	drops := regexp.MustCompile(`(?m)^path drops toward the client: 0 by the queue, ([1-9][0-9]*) scripted$`).FindStringSubmatch(raw)
	if drops == nil {
		t.Fatal("expected scripted loss and no queue drops")
	}
}
