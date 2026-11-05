// The route tests need to create and restore network namespaces. Compare the
// worker's normal unshare with a process that owns its own network namespace.
package main

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"runtime"
	"syscall"
)

func fail(stage string, err error) {
	var errno syscall.Errno
	if errors.As(err, &errno) {
		fmt.Printf("ROUTE_PROBE %s errno=%d\n", stage, int(errno))
	} else {
		fmt.Printf("ROUTE_PROBE %s error\n", stage)
	}
	os.Exit(1)
}

func namespace(name string) string {
	path, err := os.Readlink("/proc/thread-self/ns/" + name)
	if err != nil {
		fail("namespace-read", err)
	}
	return path
}

func main() {
	switch os.Args[1] {
	case "--plain":
		runtime.LockOSThread()
		before := namespace("net")
		if err := syscall.Unshare(syscall.CLONE_NEWNET); err != nil {
			fail("plain-unshare-net", err)
		}
		fmt.Printf("ROUTE_PROBE plain-unshare-net changed=%t\n", namespace("net") != before)
		return
	case "--mapped":
		fmt.Println("ROUTE_PROBE mapped-user started")
		return
	case "--test":
		if os.Getuid() != 0 || namespace("user") == os.Args[2] || namespace("net") == os.Args[3] {
			fail("private-namespace-identity", syscall.EINVAL)
		}
		fmt.Println("ROUTE_PROBE private-test started")
		if err := syscall.Exec(os.Args[4], os.Args[4:], os.Environ()); err != nil {
			fail("test-exec", err)
		}
	}

	// Each probe runs in its own process. Even when the ordinary unshare
	// succeeds, it cannot change the namespace inherited by the next probe.
	plain := exec.Command("/proc/self/exe", "--plain")
	plain.Stdout, plain.Stderr = os.Stdout, os.Stderr
	if err := plain.Run(); err != nil {
		var exit *exec.ExitError
		if !errors.As(err, &exit) {
			fail("plain-start", err)
		}
	}
	mapping := &syscall.SysProcAttr{
		Cloneflags:                 syscall.CLONE_NEWUSER,
		UidMappings:                []syscall.SysProcIDMap{{ContainerID: 0, HostID: os.Getuid(), Size: 1}},
		GidMappings:                []syscall.SysProcIDMap{{ContainerID: 0, HostID: os.Getgid(), Size: 1}},
		GidMappingsEnableSetgroups: false,
	}
	mapped := exec.Command("/proc/self/exe", "--mapped")
	mapped.SysProcAttr = mapping
	mapped.Stdout, mapped.Stderr = os.Stdout, os.Stderr
	if err := mapped.Run(); err != nil {
		var errno syscall.Errno
		if !errors.As(err, &errno) {
			fail("mapped-user", err)
		}
		fmt.Printf("ROUTE_PROBE mapped-user errno=%d\n", int(errno))
	}

	// The initial netns also belongs to the new userns so each test can
	// setns back to it after examining its own links and routes.
	mapping.Cloneflags |= syscall.CLONE_NEWNET
	args := append([]string{"--test", namespace("user"), namespace("net")}, os.Args[1:]...)
	test := exec.Command("/proc/self/exe", args...)
	test.SysProcAttr = mapping
	test.Stdout, test.Stderr = os.Stdout, os.Stderr
	if err := test.Run(); err != nil {
		var exit *exec.ExitError
		if errors.As(err, &exit) {
			os.Exit(exit.ExitCode())
		}
		fail("private-test-start", err)
	}
}
