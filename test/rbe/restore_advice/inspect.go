// inspect checks the real checkpoint before the cold and warm trials.
package main

import (
	"fmt"
	"io"
	"log"
	"os"

	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/state/statefile"
	"gvisor.dev/gvisor/pkg/state/wire"
)

func main() {
	f, err := os.Open(os.Args[1])
	if err != nil {
		log.Fatal(err)
	}
	r, metadata, err := statefile.NewReader(f, nil)
	if err != nil {
		log.Fatal(err)
	}
	defer r.Close()
	if codec := metadata["compression"]; codec != "none" {
		log.Fatalf("unexpected checkpoint codec %q", codec)
	}
	wr := wire.Reader{Reader: r}
	for _, name := range []string{"cpuid", "kernel"} {
		h, err := state.ReadHeaderMode(&wr)
		if err != nil || !h.Object || !h.Graph || h.Length == 0 {
			log.Fatalf("invalid %s graph frame %+v: %v", name, h, err)
		}
		if _, err := io.CopyN(io.Discard, r, int64(h.Length)); err != nil {
			log.Fatalf("reading %s graph: %v", name, err)
		}
		fmt.Printf("%s %d\n", name, h.Length)
	}
	if err := r.Verify(); err != nil {
		log.Fatal(err)
	}
}
