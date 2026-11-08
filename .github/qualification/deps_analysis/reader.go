// This fork-only control displays the same result in a shell-free test process.
package main

import (
	"fmt"
	"os"
)

func main() {
	report, err := os.ReadFile(os.Getenv("TEST_DEPS_REPORT"))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	if len(report) != 0 {
		os.Stdout.Write(report)
		os.Exit(1)
	}
}
