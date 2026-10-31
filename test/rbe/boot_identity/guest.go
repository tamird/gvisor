package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

func read(path string) string {
	b, err := os.ReadFile(path)
	if err != nil {
		panic(err)
	}
	return strings.TrimSpace(string(b))
}

func main() {
	if len(os.Args) == 1 {
		for {
			time.Sleep(time.Hour)
		}
	}
	if len(os.Args) == 4 && os.Args[1] == "--mount" {
        if err := syscall.Mount("proc", os.Args[3], "proc", syscall.MS_NOSUID|syscall.MS_NODEV|syscall.MS_NOEXEC, ""); err != nil {
            panic(err)
        }
        os.Args = os.Args[1:]
    }
    if len(os.Args) != 3 {
		panic("need two proc mounts")
	}
	paths := os.Args[1:]
	ids := make(map[string]string)
	for _, line := range strings.Split(read("/proc/self/mountinfo"), "\n") {
		fields := strings.Fields(line)
		if len(fields) < 10 {
			panic("invalid mountinfo")
		}
		for _, path := range paths {
			if fields[4] == path {
				if !strings.Contains(line, " - proc ") {
					panic(fmt.Sprintf("%s is not a proc mount", path))
				}
				ids[path] = fields[0]
			}
		}
	}
	if len(ids) != 2 || ids[paths[0]] == ids[paths[1]] {
		panic("need distinct mount IDs")
	}
	var boot, repeated, random []string
	for _, path := range paths {
		dir := filepath.Join(path, "sys/kernel/random")
		boot = append(boot, read(filepath.Join(dir, "boot_id")))
		repeated = append(repeated, read(filepath.Join(dir, "boot_id")))
		random = append(random, read(filepath.Join(dir, "uuid")), read(filepath.Join(dir, "uuid")))
	}
	if err := json.NewEncoder(os.Stdout).Encode(map[string][]string{"boot": boot, "repeated": repeated, "random": random}); err != nil {
		panic(err)
	}
}
