package tun

import (
	"testing"

	"gvisor.dev/gvisor/pkg/context"
	"gvisor.dev/gvisor/pkg/tcpip/link/channel"
)

// Hold the open file’s original reference across both operations.
func TestSetPersistentWhileDeleting(t *testing.T) {
	e := &tunEndpoint{Endpoint: channel.New(1, 1500, "")}
	e.InitRefs()
	result := make(chan any, 1)
	go func() {
		defer func() { result <- recover() }()
		e.setPersistent(true)
	}()
	<-persistPublished
	e.Close()
	close(resumePersist)
	if p := <-result; p != nil {
		t.Fatalf("TUNSETPERSIST panicked with an open file: %v", p)
	}
	if got, want := e.ReadRefs(), int64(1); got != want {
		t.Fatalf("references after deleting link: got %d, want %d", got, want)
	}
	e.DecRef(context.Background())
}
