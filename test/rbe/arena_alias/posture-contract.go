package sandboxposture

import (
	"io"
	"os"
	"os/exec"
	"slices"
	"testing"
)

func TestArenaSubject(t *testing.T) {
	io.Copy(io.Discard, os.Stdin)
}

func TestArenaPosture(t *testing.T) {
	const rseq = "GLIBC_TUNABLES=glibc.pthread.rseq=0"
	const tunables = "MALLOC_ARENA_MAX="
	ref := &proc{root: "/proc", pid: os.Getpid()}
	refStatus, err := ref.status()
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name    string
		env     []string
		cgo     bool
		network string
		match   bool
	}{
		{"pure", []string{}, false, "sandbox", true},
		{"cgo", []string{rseq, tunables + "32"}, true, "sandbox", true},
		{"plugin", []string{rseq, tunables + "8"}, false, "plugin", true},
		{"missing_rseq", []string{tunables + "32"}, true, "sandbox", false},
		{"missing", []string{}, true, "sandbox", false},
		{"unexpected_pure", []string{rseq, tunables + "8"}, false, "sandbox", false},
		{"old_rseq_only", []string{"GLIBC_TUNABLES=glibc.pthread.rseq=0"}, true, "sandbox", false},
		{"malformed", []string{rseq, tunables + "09"}, true, "sandbox", false},
		{"extra_tunable", []string{rseq, tunables + "32:glibc.malloc.check=0"}, true, "sandbox", false},
		{"extra_key", []string{rseq, tunables + "32", "EXTRA_KEY=1"}, true, "sandbox", false},
		{"literal_placeholder", []string{rseq, tunables + "<8*launcher-cpus>"}, true, "sandbox", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cmd := exec.Command(os.Args[0], "-test.run=^TestArenaSubject$")
			cmd.Env = tc.env
			w, err := cmd.StdinPipe()
			if err != nil {
				t.Fatal(err)
			}
			if err := cmd.Start(); err != nil {
				t.Fatal(err)
			}
			defer func() { w.Close(); cmd.Wait() }()
			p := &proc{root: "/proc", pid: cmd.Process.Pid}
			status, err := p.status()
			if err != nil {
				t.Fatal(err)
			}
			got, err := collectAttrs(p, ref, status, refStatus, NSInherited)
			if err != nil {
				t.Fatal(err)
			}
			want := expectedAttrs(ExpectedOpts{CgoEnabled: tc.cgo, NetworkMode: tc.network}, false, false)
			if slices.Equal(got.Environ, want.Environ) != tc.match {
				t.Fatalf("collected %q; expected %q; want match=%t", got.Environ, want.Environ, tc.match)
			}
		})
	}
}
