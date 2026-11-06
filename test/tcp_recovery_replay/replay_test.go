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
	if arm != "master" && arm != "pr" {
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
	for _, s := range []int{1, 2, 3, 5, 9, 12, 4, 6, 10, 14, 15, 7} {
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
