package caresqualification

import (
	"bytes"
	"context"
	"crypto/sha256"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"gvisor.dev/gvisor/pkg/test/testutil"
	pb "gvisor.dev/gvisor/test/packetimpact/proto/posix_server_go_proto"
)

func TestPOSIXSocketAndClose(t *testing.T) {
	path, err := testutil.FindFile("test/packetimpact/dut/posix_server")
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := listener.Addr().(*net.TCPAddr)
	if err := listener.Close(); err != nil {
		t.Fatal(err)
	}
	var stderr bytes.Buffer
	server := exec.Command(path, "--ip", addr.IP.String(), "--port", strconv.Itoa(addr.Port))
	server.Stderr = &stderr
	if err := server.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		server.Process.Kill()
		server.Wait()
		if t.Failed() {
			t.Log(stderr.String())
		}
	})
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	conn, err := grpc.NewClient(addr.String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	client := pb.NewPosixClient(conn)
	socket, err := client.Socket(ctx, &pb.SocketRequest{Domain: syscall.AF_INET, Type: syscall.SOCK_STREAM}, grpc.WaitForReady(true))
	if err != nil {
		t.Fatal(err)
	}
	if socket.GetErrno_() != 0 || socket.GetFd() < 3 {
		t.Fatalf("Socket returned %v", socket)
	}
	fdPath := fmt.Sprintf("/proc/%d/fd/%d", server.Process.Pid, socket.GetFd())
	target, err := os.Readlink(fdPath)
	if err != nil || !strings.HasPrefix(target, "socket:[") {
		t.Fatalf("server fd %d = %q, err %v", socket.GetFd(), target, err)
	}
	closed, err := client.Close(ctx, &pb.CloseRequest{Fd: socket.GetFd()})
	if err != nil || closed.GetRet() != 0 || closed.GetErrno_() != 0 {
		t.Fatalf("Close returned %v, err %v", closed, err)
	}
	after, err := os.Readlink(fdPath)
	if err != nil && !os.IsNotExist(err) {
		t.Fatalf("checking the released socket: %v", err)
	}
	if err == nil && after == target {
		t.Fatalf("server still owns %s after successful Close", target)
	}
	exe, err := os.Open(fmt.Sprintf("/proc/%d/exe", server.Process.Pid))
	if err != nil {
		t.Fatal(err)
	}
	defer exe.Close()
	sum := sha256.New()
	if _, err := io.Copy(sum, exe); err != nil {
		t.Fatal(err)
	}
	t.Logf("Verified POSIX Socket and Close against the compiled C++ gRPC server sha256=%x", sum.Sum(nil))
}
