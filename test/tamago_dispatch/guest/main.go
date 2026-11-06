package main

import (
	"runtime"

	_ "github.com/usbarmory/tamago/board/qemu/microvm"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
)

func main() {
	runtime.GOMAXPROCS(1)
	println("DISPATCH BOOT", runtime.GOOS, runtime.GOARCH, runtime.Version(), runtime.GOMAXPROCS(0))
	tcp.ObserveProcessorProgress(false)
	println("DISPATCH control DONE")
	tcp.ObserveProcessorProgress(true)
	println("DISPATCH contended DONE")
}
