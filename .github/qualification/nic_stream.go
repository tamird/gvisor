package gonet

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net"
	"os"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/link/fdbased"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv4"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
)

const tcpReadUnit = 1 << 20

func qualifiedTCPStream(t testing.TB) (*TCPConn, net.Conn) {
	t.Helper()
	var affinity unix.CPUSet
	if err := unix.SchedGetaffinity(0, &affinity); err != nil {
		t.Fatal(err)
	}
	if affinity.Count() != 2 || runtime.GOMAXPROCS(0) != 2 {
		t.Fatalf("workers=%d processors=%d", affinity.Count(), runtime.GOMAXPROCS(0))
	}
	for _, item := range strings.Split(os.Getenv("TCP_BENCH_CPUS"), ",") {
		cpu, err := strconv.Atoi(item)
		if err != nil || !affinity.IsSet(cpu) {
			t.Fatalf("CPU %q outside fixed affinity mask", item)
		}
	}
	fds, err := unix.Socketpair(unix.AF_UNIX, unix.SOCK_SEQPACKET|unix.SOCK_CLOEXEC, 0)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		for _, fd := range fds {
			if err := unix.Close(fd); err != nil {
				t.Error(err)
			}
		}
	})
	var networks []*stack.Stack
	t.Cleanup(func() {
		for _, s := range networks {
			s.Close()
		}
		for _, s := range networks {
			s.Wait()
		}
	})
	for i, fd := range fds {
		for _, option := range []int{unix.SO_SNDBUF, unix.SO_RCVBUF} {
			if err := unix.SetsockoptInt(fd, unix.SOL_SOCKET, option, 1<<20); err != nil {
				t.Fatal(err)
			}
		}
		ep, err := fdbased.New(&fdbased.Options{FDs: []int{fd}, MTU: 1500, PacketDispatchMode: fdbased.RecvMMsg, ProcessorsPerChannel: 1})
		if err != nil {
			t.Fatal(err)
		}
		s := stack.New(stack.Options{
			NetworkProtocols:   []stack.NetworkProtocolFactory{ipv4.NewProtocol},
			TransportProtocols: []stack.TransportProtocolFactory{tcp.NewProtocol},
		})
		networks = append(networks, s)
		if err := s.CreateNIC(1, ep); err != nil {
			t.Fatal(err)
		}
		addr := tcpip.AddrFrom4([4]byte{192, 0, 2, byte(i + 1)})
		if err := s.AddProtocolAddress(1, tcpip.ProtocolAddress{Protocol: ipv4.ProtocolNumber, AddressWithPrefix: addr.WithPrefix()}, stack.AddressProperties{}); err != nil {
			t.Fatal(err)
		}
		s.SetRouteTable([]tcpip.Route{{Destination: header.IPv4EmptySubnet, NIC: 1}})
	}
	server := tcpip.FullAddress{NIC: 1, Addr: tcpip.AddrFrom4([4]byte{192, 0, 2, 2}), Port: 9012}
	l, err := ListenTCP(networks[1], server, ipv4.ProtocolNumber)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { l.Close() })
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	writer, err := DialContextTCP(ctx, networks[0], server, ipv4.ProtocolNumber)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { writer.Close() })
	reader, err := l.Accept()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { reader.Close() })
	for _, c := range []net.Conn{writer, reader} {
		if err := c.SetDeadline(time.Now().Add(45 * time.Second)); err != nil {
			t.Fatal(err)
		}
	}
	return writer, reader
}

func streamPayload(size int) []byte {
	out := make([]byte, size)
	x := uint32(0x476f2049)
	for i := range out {
		x ^= x << 13
		x ^= x >> 17
		x ^= x << 5
		out[i] = byte(x)
	}
	return out
}

func transferTCPStream(t testing.TB, writer *TCPConn, reader net.Conn, src, dst []byte, readSize int) int {
	t.Helper()
	wrote := make(chan error, 1)
	go func() {
		for off := 0; off < len(src); {
			n, err := writer.Write(src[off:min(off+(64<<10), len(src))])
			if err != nil {
				wrote <- err
				return
			}
			if n == 0 {
				wrote <- io.ErrNoProgress
				return
			}
			off += n
		}
		wrote <- nil
	}()
	reads := 0
	for off := 0; off < len(dst); {
		n, err := reader.Read(dst[off:min(off+readSize, len(dst))])
		if err != nil || n == 0 {
			t.Fatalf("read %d/%d: n=%d err=%v", off, len(dst), n, err)
		}
		reads++
		off += n
	}
	if err := <-wrote; err != nil {
		t.Fatal(err)
	}
	return reads
}

func warmTCPStream(t testing.TB, writer *TCPConn, reader net.Conn, size int) {
	t.Helper()
	data := streamPayload(256 << 10)
	got := make([]byte, len(data))
	transferTCPStream(t, writer, reader, data, got, size)
	if !bytes.Equal(data, got) {
		t.Fatal("warm-up byte mismatch")
	}
}

func checkTCPStream(t testing.TB, writer *TCPConn, reader net.Conn, src, dst []byte) {
	t.Helper()
	if err := writer.CloseWrite(); err != nil {
		t.Fatal(err)
	}
	var extra [1]byte
	if n, err := reader.Read(extra[:]); n != 0 || err != io.EOF {
		t.Fatalf("extra data after %d bytes: %d, %v", len(src), n, err)
	}
	if !bytes.Equal(src, dst) {
		t.Fatalf("byte mismatch in %d-byte transfer", len(src))
	}
}

func TestConnectedTCPReadTiming(t *testing.T) {
	for _, size := range []int{64, 64 << 10} {
		t.Run(fmt.Sprintf("read=%d", size), func(t *testing.T) {
			t.Log("connecting")
			writer, reader := qualifiedTCPStream(t)
			t.Log("warming the connection")
			warmTCPStream(t, writer, reader, size)
			const units = 4
			data := streamPayload(units * tcpReadUnit)
			got := make([]byte, len(data))
			t.Log("starting timed transfer")
			start := time.Now()
			calls := transferTCPStream(t, writer, reader, data, got, size)
			elapsed := time.Since(start)
			t.Log("checking bytes and EOF")
			checkTCPStream(t, writer, reader, data, got)
			result := testing.BenchmarkResult{
				N:     units,
				T:     elapsed,
				Bytes: tcpReadUnit,
				Extra: map[string]float64{"B/read": float64(len(data)) / float64(calls)},
			}
			fmt.Printf("BenchmarkConnectedTCPRead/read=%d-2\t%s\n", size, result.String())
			t.Log("transfer verified")
		})
	}
}
