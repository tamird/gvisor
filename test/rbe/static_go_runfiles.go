// Fork-only probe: the same lookup and copy used by the container boot tests.
package main

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"syscall"

	"gvisor.dev/gvisor/pkg/test/testutil"
)

func main() {
	cwd, err := os.Getwd()
	if err != nil {
		panic(err)
	}
	path, err := testutil.FindFile("test/cmd/test_app/test_app")
	if err != nil {
		panic(err)
	}
	rel, err := filepath.Rel(cwd, path)
	if err != nil {
		panic(err)
	}
	err = testutil.Copy(path, os.Args[1])
	code := 0
	if err != nil {
		var errno syscall.Errno
		if !errors.As(err, &errno) {
			panic(err)
		}
		code = int(errno)
	}
	if err := json.NewEncoder(os.Stdout).Encode(map[string]any{"path": rel, "copy_errno": code}); err != nil {
		panic(err)
	}
	if code != 0 {
		os.Exit(1)
	}
}
