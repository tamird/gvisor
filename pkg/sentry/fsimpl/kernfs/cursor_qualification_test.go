package kernfs

import (
	"testing"

	"gvisor.dev/gvisor/pkg/sentry/vfs"
)

// +checklocksread:o.mu
func removeDuringReadForQualification(o *OrderedChildren) {
	o.mu.RUnlock()
	o.mu.Lock()
	o.removeLocked("first")
	o.mu.Unlock()
	o.mu.RLock()
}

func walkForQualification(o *OrderedChildren, mode string, cb vfs.IterDirentsCallback) (string, string, error) {
	o.mu.RLock()
	defer o.mu.RUnlock()
	it := o.nthLocked(0)
	if mode == "direct" {
		o.mu.RUnlock()
		o.mu.Lock()
		o.removeLocked("first")
		o.mu.Unlock()
		o.mu.RLock()
	}
	if mode == "helper" {
		removeDuringReadForQualification(o)
	}
	if err := cb.Handle(vfs.Dirent{Name: it.name}); err != nil {
		return "", "", err
	}
	var nextName string
	if next := it.Next(); next != nil {
		nextName = next.name
	}
	return nextName, o.nthLocked(0).name, nil
}

func TestKernfsCursorQualification(t *testing.T) {
	for _, mode := range []string{"preserve", "direct", "helper", "callback"} {
		t.Run(mode, func(t *testing.T) {
			var o OrderedChildren
			for _, name := range []string{"first", "second"} {
				if err := o.insert(name, nil, false); err != nil {
					t.Fatal(err)
				}
			}
			cb := vfs.IterDirentsCallbackFunc(func(vfs.Dirent) error {
				if mode == "callback" {
					removeDuringReadForQualification(&o)
				}
				return nil
			})
			next, first, err := walkForQualification(&o, mode, cb)
			if err != nil {
				t.Fatal(err)
			}
			wantNext, wantFirst := "", "second"
			if mode == "preserve" {
				wantNext, wantFirst = "second", "first"
			}
			if next != wantNext || first != wantFirst {
				t.Fatalf("saved.Next() = %q, fresh head = %q; want %q, %q", next, first, wantNext, wantFirst)
			}
			t.Logf("saved.Next() = %q, fresh head = %q", next, first)
		})
	}
}
