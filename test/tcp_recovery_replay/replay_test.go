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
	if arm != "pr" && arm != "timeout_counting" {
		t.Fatal("unknown source arm")
	}
	run := func(t *testing.T, seed int, burst bool) (deadline bool) {
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
			t.Fatalf("%s seed=%d burst=%t: %v (context: %v)", arm, seed, burst, err, ctx.Err())
		}
		start := []byte("TCP_MEASURE_START\n")
		if bytes.Count(out, start) != 1 {
			t.Fatal("expected exactly one measured transfer start")
		}
		_, measured, _ := bytes.Cut(out, start)
		during, _, found := bytes.Cut(measured, []byte("TCP_REPLAY_RESULT "))
		if !found {
			t.Fatal("missing measured transfer result")
		}
		var got int
		for _, line := range bytes.Split(measured, []byte{'\n'}) {
			payload, isResult := bytes.CutPrefix(line, []byte("TCP_REPLAY_RESULT "))
			if !isResult {
				continue
			}
			got++
			var detailed map[string]json.RawMessage
			if err := json.Unmarshal(payload, &detailed); err != nil {
				t.Fatal(err)
			}
			witnesses := []map[string]int64{}
			for _, recorded := range bytes.Split(during, []byte{'\n'}) {
				raw, hit := bytes.CutPrefix(recorded, []byte("TCP_RETRY_LIMIT "))
				if !hit {
					continue
				}
				var w map[string]int64
				if err := json.Unmarshal(raw, &w); err != nil {
					t.Fatal(err)
				}
				var base uint32
				if err := json.Unmarshal(detailed["from_server_seq"], &base); err != nil {
					t.Fatal(err)
				}
				w["sequence_offset"] = int64(uint32(w["sequence"]) - base)
				w["una_offset"] = int64(uint32(w["una"]) - base)
				witnesses = append(witnesses, w)
			}
			detailed["retry_limit_witness"], err = json.Marshal(witnesses)
			if err != nil {
				t.Fatal(err)
			}
			payload, err = json.Marshal(detailed)
			if err != nil {
				t.Fatal(err)
			}
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
				if !burst {
					t.Fatal("no-loss transfer reached its read deadline")
				}
			} else if x.Bytes != x.Requested || x.Missing != 0 || x.Calls != 3 || len(x.Server) == 0 || len(x.Peer) == 0 {
				t.Fatal("success did not account for every response byte")
			}
		}
		if got != 1 {
			t.Fatalf("expected one measured transfer, found %d", got)
		}
		return deadline
	}
	complete, stalled := 0, 0
	for _, trial := range []struct {
		seed  int
		burst bool
	}{{2, false}, {2, true}, {3, true}, {5, true}, {9, true}, {12, true}, {15, true}} {
		loss := "none"
		if trial.burst {
			loss = "burst"
		}
		t.Run(fmt.Sprintf("%s_%d", loss, trial.seed), func(t *testing.T) {
			if run(t, trial.seed, trial.burst) {
				stalled++
			} else {
				complete++
			}
		})
	}
	fmt.Printf("TCP_COUNTS arm=%s complete=%d deadline=%d\n", arm, complete, stalled)
}
