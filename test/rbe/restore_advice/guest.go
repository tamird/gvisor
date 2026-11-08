// This qualification guest keeps application pages and bytes in kernel pipes
// separate, so restoring the small kernel image cannot stand in for restoring
// the application's memory.
package main

import (
	"crypto/sha256"
	"fmt"
	"io"
	"math/rand"
	"net"
	"os"

	"golang.org/x/sys/unix"
)

func check(err error) {
	if err != nil {
		panic(err)
	}
}

const address = "@restore-advice"
const mib = 1024 * 1024

func serve() {
	payload := make([]byte, 512*mib)
	rng := rand.New(rand.NewSource(29))
	_, err := rng.Read(payload)
	check(err)
	var readers [8]*os.File
	for i := range readers {
		r, w, err := os.Pipe()
		check(err)
		capacity, err := unix.FcntlInt(w.Fd(), unix.F_SETPIPE_SZ, mib)
		check(err)
		if capacity < mib {
			panic(capacity)
		}
		b := payload[i*mib : (i+1)*mib]
		n, err := w.Write(b)
		check(err)
		if n != len(b) {
			panic(n)
		}
		check(w.Close())
		readers[i] = r
	}
	prefix := sha256.Sum256(payload[:len(readers)*mib])
	entire := sha256.Sum256(payload)
	listener, err := net.Listen("unix", address)
	check(err)
	for {
		c, err := listener.Accept()
		check(err)
		var op [1]byte
		_, err = io.ReadFull(c, op[:])
		check(err)
		switch op[0] {
		case 'r':
			_, err = fmt.Fprintf(c, "%x %x\n", entire, prefix)
		case 'v':
			h := sha256.New()
			for _, r := range readers {
				n, err := io.Copy(h, r)
				check(err)
				if n != mib {
					panic(n)
				}
				check(r.Close())
			}
			_, err = fmt.Fprintf(c, "%x %x\n", sha256.Sum256(payload), h.Sum(nil))
		default:
			panic(op)
		}
		check(err)
		check(c.Close())
	}
}

func main() {
	if len(os.Args) == 1 {
		serve()
		return
	}
	if os.Args[1] != "ready" && os.Args[1] != "verify" {
		panic(os.Args[1])
	}
	c, err := net.Dial("unix", address)
	check(err)
	defer c.Close()
	_, err = c.Write([]byte{os.Args[1][0]})
	check(err)
	_, err = io.Copy(os.Stdout, c)
	check(err)
}
