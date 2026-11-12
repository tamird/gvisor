package remote

import (
	"bytes"
	"fmt"
	"os"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/anypb"
	"gvisor.dev/gvisor/pkg/fd"
	"gvisor.dev/gvisor/pkg/log"
	pb "gvisor.dev/gvisor/pkg/sentry/seccheck/points/points_go_proto"
	"gvisor.dev/gvisor/pkg/sentry/seccheck/sinks/remote/wire"
)

// A private socketpair carries opaque padding and one synthetic marker.
// Once the first nonblocking send observes backpressure, the test consumer
// drains the padding before the real retry loop decides whether to try again.
type resumeReader struct {
	t       *testing.T
	peer    int
	drained int
}

func (r *resumeReader) Emit(_ int, _ log.Level, _ time.Time, format string, args ...any) {
	if !strings.HasPrefix(format, "Write failed, ") {
		return
	}
	if r.drained != 0 {
		r.t.Fatalf("send failed again after receiver drained: %s", fmt.Sprintf(format, args...))
	}
	r.t.Logf("consumer resumed on first send result: %s", fmt.Sprintf(format, args...))
	var buf [32]byte
	for {
		n, err := syscall.Read(r.peer, buf[:])
		if err == syscall.EAGAIN {
			return
		}
		if err != nil || !bytes.Equal(buf[:n], []byte("padding")) {
			r.t.Fatalf("unexpected synthetic padding: n=%d err=%v", n, err)
		}
		r.drained++
	}
}

func TestIsolatedTraceSinkRetry(t *testing.T) {
	retries, err := strconv.Atoi(os.Getenv("QUALIFICATION_TRACE_RETRIES"))
	if err != nil || retries < 0 || retries > 32 {
		t.Fatalf("invalid retry count: %d (%v)", retries, err)
	}
	sockets, err := syscall.Socketpair(syscall.AF_UNIX, syscall.SOCK_SEQPACKET|syscall.SOCK_CLOEXEC|syscall.SOCK_NONBLOCK, 0)
	if err != nil {
		t.Fatal(err)
	}
	sender := fd.New(sockets[0])
	defer sender.Close()
	defer syscall.Close(sockets[1])
	config := map[string]any{}
	if retries != 0 {
		config["retries"] = float64(retries)
	}
	sink, err := new(config, sender)
	if err != nil {
		t.Fatal(err)
	}
	reader := &resumeReader{t: t, peer: sockets[1]}
	old := log.Log()
	log.SetTarget(reader)
	log.SetLevel(log.Debug)
	defer func() {
		log.SetTarget(old.Emitter)
		log.SetLevel(old.Level)
	}()
	padding := 0
	for {
		_, err := syscall.Write(sockets[0], []byte("padding"))
		if err == syscall.EAGAIN {
			break
		}
		if err != nil || padding >= 65536 {
			t.Fatalf("could not establish a bounded synthetic control: %d writes, error %v", padding, err)
		}
		padding++
	}
	marker := &anypb.Any{Value: []byte("ordinary qualification marker")}
	sink.(*remote).write(marker, pb.MessageType(0))
	if reader.drained != padding || reader.drained == 0 {
		t.Fatalf("drained %d packets, wrote %d padding packets", reader.drained, padding)
	}
	var packet [1024]byte
	n, err := syscall.Read(sockets[1], packet[:])
	dropped := sink.Status().DroppedCount
	if retries == 0 {
		if err != syscall.EAGAIN || dropped != 1 {
			t.Fatalf("no-retry control read %d bytes (%v), dropped %d", n, err, dropped)
		}
		t.Log("synthetic marker absent, dropped=1")
		return
	}
	if err != nil || n < wire.HeaderStructSize || dropped != 0 {
		t.Fatalf("retry control read %d bytes (%v), dropped %d", n, err, dropped)
	}
	var decoded anypb.Any
	if err := proto.Unmarshal(packet[wire.HeaderStructSize:n], &decoded); err != nil || !proto.Equal(&decoded, marker) {
		t.Fatalf("synthetic marker mismatch: %v, error %v", &decoded, err)
	}
	t.Log("synthetic marker received intact, dropped=0")
}
