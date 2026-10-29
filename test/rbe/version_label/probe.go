package main

import (
	"fmt"
	"os"

	"gvisor.dev/gvisor/runsc/version"
)

func main() {
	fmt.Printf("reported=%s matches=%t\n", version.Version(), version.Matches(os.Args[1]))
}
