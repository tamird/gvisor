// A resident process for the real runsc checkpoint.
package main

import (
	"fmt"
	"os"
	"time"
)

func main() {
	if len(os.Args) > 1 {
		fmt.Println("restored guest executed")
		return
	}
	for {
		time.Sleep(24 * time.Hour)
	}
}
