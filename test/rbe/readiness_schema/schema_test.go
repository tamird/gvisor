package schema_test

import (
	"bytes"
	"context"
	"encoding/base64"
	"os"
	"slices"
	"strings"
	"testing"

	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
)

func TestRegisteredEndpointCheckpointBoundary(t *testing.T) {
	fields := (*tcp.Endpoint)(nil).StateFields()
	previous := os.Getenv("PARENT_STREAM")
	if got, want := slices.Contains(fields, "connectionDirectionState"), previous == ""; got != want {
		t.Fatalf("connectionDirectionState in generated fields = %v, want %v", got, want)
	}
	if !slices.Contains(fields, "shutdownFlags") || !state.GraphEngineEnabled() {
		t.Fatal("expected saved shutdownFlags and graph codec")
	}

	ctx := context.Background()
	var buf bytes.Buffer
	const value uint64 = 0x112233445566
	original := value
	if _, err := state.Save(ctx, &buf, &original); err != nil {
		t.Fatal(err)
	}
	var restored uint64
	if _, err := state.Load(ctx, bytes.NewReader(buf.Bytes()), &restored); err != nil || restored != value {
		t.Fatalf("same-binary restore = %x, %v", restored, err)
	}
	t.Logf("schema-receipt fields=%d cached_field=%t stream=%s", len(fields), previous == "", base64.StdEncoding.EncodeToString(buf.Bytes()))
	if previous == "" {
		return
	}
	b, err := base64.StdEncoding.DecodeString(previous)
	if err != nil {
		t.Fatal(err)
	}
	var destination uint64
	_, err = state.Load(ctx, bytes.NewReader(b), &destination)
	if err == nil || !strings.Contains(err.Error(), "saved by a binary with different types (fingerprint") || destination != 0 {
		t.Fatalf("cross-binary restore returned %v, wrote %x", err, destination)
	}
	t.Log("parent stream rejected before decoding destination")
}
