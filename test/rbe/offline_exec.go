//go:build linux

// Check the kernel route inside this test's actual worker, then run the test.
package main

import (
	"errors"
	"fmt"
	"os"
	"syscall"
)

func main() {
	if len(os.Args) < 2 {
		panic("missing test executable")
	}
	fd, err := syscall.Socket(syscall.AF_INET, syscall.SOCK_STREAM|syscall.SOCK_NONBLOCK|syscall.SOCK_CLOEXEC, 0)
	if err != nil {
		panic(err)
	}
	// RFC 5737 reserves this address for documentation. A connected worker
	// with a default route returns EINPROGRESS. A worker without a route
	// rejects the connect immediately, before any packet leaves the host.
	err = syscall.Connect(fd, &syscall.SockaddrInet4{Addr: [4]byte{192, 0, 2, 1}, Port: 1})
	if closeErr := syscall.Close(fd); closeErr != nil {
		panic(closeErr)
	}
	if !errors.Is(err, syscall.ENETUNREACH) {
		panic(fmt.Sprintf("worker connect got %v, want ENETUNREACH", err))
	}
	fmt.Fprintln(os.Stderr, "offline worker: route rejected with ENETUNREACH")
	if err := syscall.Exec(os.Args[1], os.Args[1:], os.Environ()); err != nil {
		panic(err)
	}
}
