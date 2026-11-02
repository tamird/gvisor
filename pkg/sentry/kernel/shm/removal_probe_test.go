package shm

import (
	"testing"

	"gvisor.dev/gvisor/pkg/abi/linux"
	"gvisor.dev/gvisor/pkg/context"
	"gvisor.dev/gvisor/pkg/errors/linuxerr"
	"gvisor.dev/gvisor/pkg/sentry/contexttest"
	"gvisor.dev/gvisor/pkg/sentry/kernel/auth"
	"gvisor.dev/gvisor/pkg/sentry/pgalloc"
)

// TestRemovalPublication holds the registry mutex after obtaining the ID
// reference that shmctl(IPC_STAT) takes before it reads the segment.
func TestRemovalPublication(t *testing.T) {
	ctx := context.WithValue(contexttest.RootContext(t), CtxDeviceID, uint32(1))
	defer pgalloc.MemoryFileFromContext(ctx).Destroy()
	r := NewRegistry(auth.CredentialsFromContext(ctx).UserNamespace)
	const key = 73
	s, err := r.FindOrCreate(ctx, 1, key, 4096, 0600, false, true, true)
	if err != nil {
		t.Fatal(err)
	}
	defer s.DecRef(ctx)
	oldID := s.Object().ID
	reader := r.FindByID(oldID)
	if reader == nil {
		t.Fatal("original ID lookup failed")
	}
	defer reader.DecRef(ctx)

	r.mu.Lock()
	attempt := make(chan struct{})
	r.mu.attempt = attempt
	done := make(chan error, 1)
	go func() { done <- s.MarkDestroyed(ctx) }()
	<-attempt
	// Until this goroutine releases r.mu, the removal cannot pass its
	// registry lock acquisition. IPCStat takes only the segment lock.
	during, statErr := reader.IPCStat(ctx)
	r.mu.attempt = nil
	r.mu.Unlock()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if statErr != nil {
		t.Fatal(statErr)
	}
	after, err := reader.IPCStat(ctx)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("IPC_STAT during removal: key=%d SHM_DEST=%t; after: key=%d SHM_DEST=%t",
		during.ShmPerm.Key, during.ShmPerm.Mode&linux.SHM_DEST != 0,
		after.ShmPerm.Key, after.ShmPerm.Mode&linux.SHM_DEST != 0)
	if after.ShmPerm.Key != linux.IPC_PRIVATE || after.ShmPerm.Mode&linux.SHM_DEST == 0 {
		t.Fatalf("unexpected final IPC_STAT: %+v", after.ShmPerm)
	}
	if _, err := r.FindOrCreate(ctx, 1, key, 0, 0600, false, false, false); !linuxerr.Equals(linuxerr.ENOENT, err) {
		t.Fatalf("old key lookup: got %v, want ENOENT", err)
	}
	replacement, err := r.FindOrCreate(ctx, 1, key, 4096, 0600, false, true, true)
	if err != nil {
		t.Fatal(err)
	}
	defer replacement.DecRef(ctx)
	if err := replacement.MarkDestroyed(ctx); err != nil {
		t.Fatal(err)
	}
	if replacement.Object().ID == oldID {
		t.Fatalf("reused ID %v while its original segment is still referenced", oldID)
	}
	if during.ShmPerm.Mode&linux.SHM_DEST != 0 || during.ShmPerm.Key != key {
		t.Error("removal published a partial IPC_STAT before acquiring the registry mutex")
	}
}
