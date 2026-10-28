// Validation fixture for the public state.SaverLoader API.
package direct

import (
	"bytes"
	"context"
	"testing"

	"gvisor.dev/gvisor/pkg/state"
)

type slots struct {
	first, second child
	distinct      bool
}

func (*slots) StateTypeName() string { return "direct.slots" }
func (*slots) StateFields() []string { return []string{"first", "second"} }
func (r *slots) StateSave(s state.Sink) {
	s.Save(0, &r.first)
	if r.distinct {
		s.Save(1, &r.second)
	} else {
		s.Save(1, &r.first)
	}
}
func (r *slots) StateLoad(_ context.Context, s state.Source) {
	s.Load(0, &r.first)
	s.Load(1, &r.second)
}

type child struct {
	value    uint64
	restored *uint64
}

func (*child) StateTypeName() string { return "direct.child" }
func (*child) StateFields() []string { return []string{"value"} }
func (c *child) StateSave(s state.Sink) {
	value := c.value
	s.SaveValue(0, &value)
}
func (c *child) StateLoad(_ context.Context, s state.Source) {
	s.Load(0, &c.restored)
}

func init() {
	state.Register((*slots)(nil))
	state.Register((*child)(nil))
}

func TestDirectStructSlots(t *testing.T) {
	for _, tc := range []struct {
		name     string
		distinct bool
	}{{"same", false}, {"distinct", true}} {
		t.Run(tc.name, func(t *testing.T) {
			r := slots{first: child{value: 73}, second: child{value: 73}, distinct: tc.distinct}
			var b bytes.Buffer
			if _, err := state.Save(t.Context(), &b, &r); err != nil {
				t.Fatal(err)
			}
			size := b.Len()
			var loaded slots
			if _, err := state.Load(t.Context(), &b, &loaded); err != nil {
				t.Fatal(err)
			}
			a, z := loaded.first.restored, loaded.second.restored
			if a == nil || z == nil || *a != 73 || *z != 73 {
				t.Fatalf("payload did not round trip: %v, %v", a, z)
			}
			t.Logf("DIRECT_SLOT case=%s same_pointer=%t first=%d second=%d bytes=%d", tc.name, a == z, *a, *z, size)
			if (a == z) == tc.distinct {
				t.Errorf("restored pointer identity differs: same_pointer=%t, distinct_input=%t", a == z, tc.distinct)
			}
		})
	}
}
