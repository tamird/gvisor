// Report the test executable visible to the real TestRunner, then execute it.
package main

import (
	"crypto/sha256"
	"debug/elf"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"syscall"
)

func main() {
	if len(os.Args) < 2 || filepath.Base(os.Args[1]) != "boot_test" {
		fmt.Fprintln(os.Stderr, "expected the declared boot_test executable")
		os.Exit(2)
	}
	// This is the exact canonical runfiles path reported by the unmodified
	// native wrapper on both workers. Bazel may pass a relative argv[1].
	target := filepath.Join(os.Getenv("TEST_SRCDIR"), "_main", "runsc/boot/boot_test_/boot_test")
	observation := map[string]any{"arg": os.Args[1], "path": target}
	if info, err := os.Lstat(target); err == nil {
		observation["mode"] = info.Mode().String()
		if info.Mode()&os.ModeSymlink != 0 {
			link, err := os.Readlink(target)
			if err == nil {
				observation["link"] = link
			}
		}
	}
	if resolved, err := filepath.EvalSymlinks(target); err == nil {
		observation["resolved"] = resolved
	}
	if f, err := os.Open(target); err == nil {
		sum := sha256.New()
		if _, err := io.Copy(sum, f); err == nil {
			observation["sha256"] = hex.EncodeToString(sum.Sum(nil))
		}
		if binary, err := elf.NewFile(f); err == nil {
			observation["machine"] = binary.Machine.String()
			libs, _ := binary.ImportedLibraries()
			observation["needed"] = libs
			observation["interp"] = ""
			for _, header := range binary.Progs {
				if header.Type != elf.PT_INTERP {
					continue
				}
				raw, err := io.ReadAll(io.LimitReader(header.Open(), 1024))
				if err == nil {
					interp := strings.TrimRight(string(raw), "\x00")
					observation["interp"] = interp
					if _, err := os.Stat(interp); err == nil {
						observation["interp_exists"] = true
					} else {
						observation["interp_error"] = err.Error()
						observation["interp_missing"] = os.IsNotExist(err)
					}
				}
			}
		}
		f.Close()
	} else {
		observation["open_error"] = err.Error()
	}
	if argInfo, argErr := os.Stat(os.Args[1]); argErr == nil {
		targetInfo, targetErr := os.Stat(target)
		observation["arg_same_file"] = targetErr == nil && os.SameFile(argInfo, targetInfo)
	}
	data, _ := json.Marshal(observation)
	fmt.Printf("BOOT_EXEC %s\n", data)
	if err := syscall.Exec(target, os.Args[1:], os.Environ()); err != nil {
		fmt.Fprintf(os.Stderr, "BOOT_EXEC_ERR %v\n", err)
		os.Exit(127)
	}
}
