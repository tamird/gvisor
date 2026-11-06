package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"testing"
	"time"
)

func TestMain(m *testing.M) {
	if os.Getenv("REPLAY_CHILD") == "yes" {
		main()
		return
	}
	os.Exit(m.Run())
}

func TestCoverageSplit(t *testing.T) {
	sent := []timedSpan{{span{0, 5}, 0}, {span{8, 10}, 0}, {span{0, 4}, 1}}
	arrived := []span{{0, 2}, {8, 9}}
	never, lost, first := wireCoverage(12, sent, arrived)
	if never != 5 || lost != 4 || len(first) != 4 || first[0] != (missingPart{span{2, 5}, true}) || first[1] != (missingPart{span{5, 8}, false}) {
		t.Fatalf("never sent=%d, emitted but absent=%d, missing intervals=%v", never, lost, first)
	}
}

func TestMeasure(t *testing.T) {
	arm := os.Getenv("TCP_REPLAY_ARM")
	if arm != "fixed" {
		t.Fatal("unknown source arm")
	}
	run := func(seed int, burst bool) (deadline bool) {
		ctx, cancel := context.WithTimeout(t.Context(), 68*time.Second)
		defer cancel()
		args := []string{"-sender=gvisor", "-profile=tailscale", "-rack=true", "-warmup=2", "-fetches=1", "-trials=1", "-timeout=30s", "-interval=3s", "-rtt=3ms", "-rate-mbps=80", "-seed=" + strconv.Itoa(seed), "-trace=true"}
		if burst {
			args = append(args, "-loss=burst", "-burst-drop=100000", "-burst-window=600ms", "-burst-keep-every=3")
		} else {
			args = append(args, "-loss=none")
		}
		cmd := exec.CommandContext(ctx, os.Args[0], args...)
		cmd.Env = append(os.Environ(), "REPLAY_CHILD=yes")
		out, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("%s seed=%d burst=%t: %v (%v): %.2000s", arm, seed, burst, err, ctx.Err(), out)
		}
		var got int
		for _, line := range bytes.Split(out, []byte{'\n'}) {
			payload, isResult := bytes.CutPrefix(line, []byte("TCP_REPLAY_RESULT "))
			if !isResult {
				continue
			}
			got++
			var x struct {
				Operation string           `json:"operation"`
				TimedOut  bool             `json:"client_timed_out"`
				Seed      int              `json:"seed"`
				Requested int              `json:"requested"`
				Bytes     int              `json:"client_read"`
				Error     string           `json:"client_error"`
				Calls     int              `json:"writer_requests"`
				Drops     int              `json:"dropped_packets"`
				Missing   int              `json:"missing_wire_bytes"`
				Overflow  bool             `json:"capture_limit"`
				Server    map[string]int64 `json:"server_after"`
				Peer      map[string]int64 `json:"peer_after"`
			}
			if err := json.Unmarshal(payload, &x); err != nil {
				t.Fatal(err)
			}
			fmt.Printf("TCP_SAMPLE arm=%s %s\n", arm, payload)
			deadline = x.Error != ""
			if x.Seed != seed || x.Requested != 2800000 || x.Overflow || (burst != (x.Drops > 0)) {
				t.Fatal("incomplete measured-transfer or endpoint identity")
			}
			if deadline {
				if !x.TimedOut || x.Operation != "response_read" || x.Calls != 3 || x.Bytes >= x.Requested {
					t.Fatal("unexpected client failure")
				}
				start := bytes.Index(out, []byte("  wire trace at the server"))
				if start >= 0 {
					remaining := out[start:]
					if len(remaining) > 12000 {
						remaining = remaining[:12000]
					}
					fmt.Printf("TCP_TIMELINE arm=%s seed=%d\n%s\n", arm, seed, remaining)
				}
			} else if x.Bytes != x.Requested || x.Missing != 0 || x.Calls != 3 || len(x.Server) == 0 || len(x.Peer) == 0 {
				t.Fatal("success did not account for every response byte")
			}
		}
		if got != 1 {
			t.Fatalf("expected one measured transfer, found %d: %.1000s", got, out)
		}
		return deadline
	}
	if run(1, false) {
		t.Fatal("loss-free control timed out")
	}
	complete, stalled := 0, 0
	// Seeds select delay jitter, not scheduling. Bound the diagnostic without
	// treating this stopping rule as a failure-rate measurement.
	for _, s := range []int{3, 4, 6, 1, 9, 12} {
		if run(s, true) {
			stalled++
		} else {
			complete++
		}
		if complete > 0 && stalled >= 2 {
			break
		}
	}
	fmt.Printf("TCP_COUNTS arm=%s complete=%d deadline=%d\n", arm, complete, stalled)
}

// TestLinuxRetry drops the original two bytes and the next emitted copy.
// The process runs under a new user and network namespace in the Bazel job.
func TestLinuxRetry(t *testing.T) {
	outer := os.Getenv("TCP_RETRY_OUTER_NETNS")
	inner, err := os.Readlink("/proc/self/ns/net")
	if err != nil || outer == "" || inner == outer {
		t.Fatal("separate network namespace required", inner, outer, err)
	}
	if err := requireOwnNetns(); err != nil {
		t.Fatal(err)
	}
	const knob = "/proc/sys/net/ipv4/tcp_retries2"
	previous, err := os.ReadFile(knob)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(knob, []byte("1"), 0600); err != nil {
		t.Fatal(err)
	}
	defer func() {
		if err := os.WriteFile(knob, previous, 0600); err != nil {
			t.Error(err)
		}
	}()
	active, err := os.ReadFile(knob)
	if err != nil || string(bytes.TrimSpace(active)) != "1" {
		t.Fatal("retry limit readback", active, err)
	}
	ctx, cancel := context.WithTimeout(t.Context(), 20*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, os.Args[0], "-sender=linux", "-size=2", "-profile=tailscale", "-warmup=1", "-fetches=1", "-trials=1", "-interval=350ms", "-timeout=8s", "-rtt=3ms", "-seed=3", "-loss=burst", "-burst-keep-first=0", "-burst-drop=2", "-burst-window=0", "-trace=true")
	cmd.Env = append(os.Environ(), "REPLAY_CHILD=yes")
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("Linux TUN sender: %v: %.2000s", err, out)
	}
	for _, line := range bytes.Split(out, []byte{'\n'}) {
		raw, ok := bytes.CutPrefix(line, []byte("TCP_REPLAY_RESULT "))
		if !ok {
			continue
		}
		var r struct {
			Read    int     `json:"client_read"`
			Sent    int     `json:"sent_packets"`
			Drops   int     `json:"dropped_packets"`
			Missing int     `json:"missing_wire_bytes"`
			Error   string  `json:"client_error"`
			RTO     []int64 `json:"observed_rtos_ms"`
		}
		if err := json.Unmarshal(raw, &r); err != nil {
			t.Fatal(err)
		}
		fmt.Printf("TCP_LINUX_RETRIES2=1 %s\n", raw)
		if r.Read != 2 || r.Sent < 3 || r.Drops != 2 || r.Error != "" || r.Missing != 0 || len(r.RTO) != 1 {
			t.Fatalf("Linux repairs = %+v, want read after two drops and one real RTO", r)
		}
		return
	}
	t.Fatalf("missing native Linux result: %.2000s", out)
}
