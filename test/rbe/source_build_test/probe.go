// Qualification helper for the declared build proof and the original launcher.
package main

import (
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"runtime"
	"strings"
)

func run(args []string) error {
	switch args[0] {
	case "mark":
		return os.WriteFile(args[1], []byte("built\n"), 0644)
	case "fail":
		return errors.New("intentional source build-test dependency failure")
	case "launch":
		program, out := args[1], args[2]
		data, err := os.ReadFile(program)
		if err != nil {
			return err
		}
		cmd := exec.Command("./" + program)
		err = cmd.Run()
		info := map[string]any{"arch": runtime.GOARCH, "sha256": fmt.Sprintf("%x", sha256.Sum256(data)),
			"bytes": len(data), "enoent": errors.Is(err, os.ErrNotExist), "exit": -1,
			"skylib": strings.TrimSpace(string(data)) == "#!/usr/bin/env bash\nexit 0"}
		if cmd.ProcessState != nil {
			info["exit"] = cmd.ProcessState.ExitCode()
		}
		for _, p := range []string{"/usr/bin/env", "/bin/sh", "/bin/bash", "/usr/bin/touch", "/bin/touch"} {
			_, e := os.Stat(p)
			info[p+"_absent"] = errors.Is(e, os.ErrNotExist)
		}
		encoded, err := json.Marshal(info)
		if err != nil {
			return err
		}
		return os.WriteFile(out, encoded, 0644)
	}
	return errors.New("unexpected fixture mode")
}

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(41)
	}
}
