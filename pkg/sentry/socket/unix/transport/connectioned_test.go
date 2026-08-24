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

package transport

import (
	"testing"

	"gvisor.dev/gvisor/pkg/abi/linux"
	"gvisor.dev/gvisor/pkg/context"
	"gvisor.dev/gvisor/pkg/syserr"
)

// testIDProvider is used synchronously by the endpoint fixture.
type testIDProvider struct {
	nextID uint64
}

// UniqueID implements uniqueid.Provider.
func (p *testIDProvider) UniqueID() uint64 {
	p.nextID++
	return p.nextID
}

func TestBidirectionalConnectUnsupportedStreamEndpoint(t *testing.T) {
	ctx := context.Background()
	var ids testIDProvider
	client := newConnectioned(ctx, linux.SOCK_STREAM, &ids)
	server := newConnectioned(ctx, linux.SOCK_STREAM, &ids)
	if err := server.Bind(Address{Addr: "server"}); err != nil {
		t.Fatal(err)
	}
	if err := server.Listen(ctx, 1); err != nil {
		t.Fatal(err)
	}
	server.Lock()
	pending := server.acceptedChan
	server.Unlock()

	// Forward every ConnectingEndpoint method, but use a different concrete type.
	wrapped := struct{ ConnectingEndpoint }{client}
	called := false
	err := server.BidirectionalConnect(ctx, wrapped, func(Receiver, Sender) {
		called = true
	})

	// Check the borrowed channel before cleanup or another endpoint operation.
	// The broken error path leaves both locks held, so Close would hang instead
	// of reporting the erroneously queued child.
	if got := len(pending); got != 0 {
		t.Fatalf("pending connections after failed connect = %d, want 0", got)
	}
	// No endpoint has been shared with a receiver goroutine.
	t.Cleanup(func() {
		client.Close(ctx)
		server.Close(ctx)
	})
	if err != syserr.ErrInvalidEndpointState {
		t.Fatalf("BidirectionalConnect() = %v, want %v", err, syserr.ErrInvalidEndpointState)
	}
	if called {
		t.Fatal("failed connect invoked its success callback")
	}

	// Both endpoints must remain usable for a normal concrete connection.
	if err := client.Connect(ctx, server); err != nil {
		t.Fatalf("Connect() after rejected endpoint: %v", err)
	}
	accepted, err := server.Accept(ctx, nil)
	if err != nil {
		t.Fatalf("Accept() after rejected endpoint: %v", err)
	}
	t.Cleanup(func() { accepted.Close(ctx) })
}
