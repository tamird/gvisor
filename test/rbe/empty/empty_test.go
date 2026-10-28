// Private differential fixture for a registered zero-size field with saved state.
package empty

import (
	"bytes"
	"context"
	"reflect"
	"testing"

	"gvisor.dev/gvisor/pkg/state"
)

type tokenKey struct{}
type restoredKey struct{}

// A session's token lives in the save context, not in Go storage.
type session struct{}

func (*session) StateTypeName() string { return "empty.session" }
func (*session) StateFields() []string { return []string{"token"} }
func (*session) StateSave(s state.Sink) {
	s.SaveValue(0, s.Context().Value(tokenKey{}).(uint64))
}
func (*session) StateLoad(ctx context.Context, s state.Source) {
	var token uint64
	s.Load(0, &token)
	s.AfterLoad(func() {
		out := ctx.Value(restoredKey{}).(*[]uint64)
		*out = append(*out, token)
	})
}

type holder struct {
	session session
	guard   uint64
}

func (*holder) StateTypeName() string { return "empty.holder" }
func (*holder) StateFields() []string { return []string{"session", "guard"} }
func (h *holder) StateSave(s state.Sink) {
	s.Save(0, &h.session)
	s.Save(1, &h.guard)
}
func (h *holder) StateLoad(_ context.Context, s state.Source) {
	s.Load(0, &h.session)
	s.Load(1, &h.guard)
}

func init() {
	state.Register((*session)(nil))
	state.Register((*holder)(nil))
}

func roundTrip(t *testing.T, original, restored any) {
	t.Helper()
	const token uint64 = 73
	var stream bytes.Buffer
	if _, err := state.Save(context.WithValue(t.Context(), tokenKey{}, token), &stream, original); err != nil {
		t.Fatalf("Save: %v", err)
	}
	var received []uint64
	if _, err := state.Load(context.WithValue(t.Context(), restoredKey{}, &received), &stream, restored); err != nil {
		t.Fatalf("Load: %v", err)
	}
	t.Logf("%s: session tokens delivered after load: %v", t.Name(), received)
	if !reflect.DeepEqual(received, []uint64{token}) {
		t.Errorf("restored session tokens = %v, want [%d]", received, token)
	}
}

func TestDirectSession(t *testing.T) {
	var restored holder
	roundTrip(t, &holder{guard: 19}, &restored)
	if restored.guard != 19 {
		t.Errorf("guard = %d, want 19", restored.guard)
	}
}

func TestInterfaceSession(t *testing.T) {
	var original any = session{}
	var restored any
	roundTrip(t, &original, &restored)
	if _, ok := restored.(session); !ok {
		t.Errorf("restored interface contains %T, want session", restored)
	}
}
