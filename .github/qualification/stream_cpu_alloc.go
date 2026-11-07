// Profiling fixture used with the original connected TCP stream test.
package gonet

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"runtime/pprof"
	"strings"
	"testing"

	"github.com/google/pprof/profile"
	"golang.org/x/sys/unix"
)

type sampled struct {
	Count int64
	Value int64
}

type report struct {
	Mode                                            string
	Bytes, Reads, ProcessCPUNs, AllocBytes, Mallocs int64
	MemProfileRate                                  int
	Groups                                          map[string]sampled
	TrimLeaves                                      map[string]sampled
	Files                                           map[string]string
}

func add(m map[string]sampled, key string, n, value int64) {
	s := m[key]
	s.Count += n
	s.Value += value
	m[key] = s
}

func readSamples(t *testing.T, r *report, filename string, raw []byte, sign int64) {
	t.Helper()
	p, err := profile.ParseData(raw)
	if err != nil {
		t.Fatal(err)
	}
	if err := p.CheckValid(); err != nil {
		t.Fatal(err)
	}
	var count, value = -1, -1
	for i, typ := range p.SampleType {
		switch typ.Type + "/" + typ.Unit {
		case "samples/count", "alloc_objects/count":
			count = i
		case "cpu/nanoseconds", "alloc_space/bytes":
			value = i
		}
	}
	if count < 0 || value < 0 {
		t.Fatalf("unexpected sample types: %v", p.SampleType)
	}
	for _, s := range p.Sample {
		var read, trim, subApply, clone, release, write bool
		leaf := ""
		for _, loc := range s.Location {
			for _, line := range loc.Line {
				if line.Function == nil {
					continue
				}
				n := line.Function.Name
				if leaf == "" {
					leaf = n
				}
				read = read || strings.Contains(n, "/stack.PacketData.ReadTo") || strings.Contains(n, "/stack.(*PacketData).ReadTo")
				trim = trim || strings.Contains(n, "/stack.PacketData.TrimFront") || strings.Contains(n, "/stack.(*PacketData).TrimFront") ||
					strings.HasSuffix(n, "/buffer.(*Buffer).TrimFront") || strings.HasSuffix(n, "/buffer.(*Buffer).advanceRead")
				subApply = subApply || strings.HasSuffix(n, "/buffer.(*Buffer).SubApply")
				clone = clone || strings.HasSuffix(n, "/buffer.(*View).Clone")
				release = release || strings.HasSuffix(n, "/buffer.(*View).Release")
				write = write || strings.HasSuffix(n, "/tcpip.(*SliceWriter).Write")
			}
		}
		n, v := sign*s.Value[count], sign*s.Value[value]
		add(r.Groups, "all", n, v)
		if !read {
			continue
		}
		add(r.Groups, "read_total", n, v)
		side := "read_without_trim"
		if trim {
			side = "trim"
			add(r.TrimLeaves, leaf, n, v)
		}
		add(r.Groups, side, n, v)
		if subApply {
			add(r.Groups, side+"/SubApply", n, v)
		}
		if clone {
			add(r.Groups, side+"/Clone", n, v)
		}
		if release {
			add(r.Groups, side+"/Release", n, v)
		}
		if write {
			add(r.Groups, side+"/SliceWriter.Write", n, v)
		}
	}
	r.Files[filename] = fmt.Sprintf("%x", sha256.Sum256(raw))
	dir := os.Getenv("TEST_UNDECLARED_OUTPUTS_DIR")
	if dir == "" {
		t.Fatal("missing test outputs directory")
	}
	if err := os.WriteFile(filepath.Join(dir, filename), raw, 0600); err != nil {
		t.Fatal(err)
	}
}

func heapSnapshot(t *testing.T) []byte {
	t.Helper()
	runtime.GC()
	runtime.GC()
	var b bytes.Buffer
	if err := pprof.Lookup("allocs").WriteTo(&b, 0); err != nil {
		t.Fatal(err)
	}
	return b.Bytes()
}

func processCPU(t *testing.T) int64 {
	t.Helper()
	var u unix.Rusage
	if err := unix.Getrusage(unix.RUSAGE_SELF, &u); err != nil {
		t.Fatal(err)
	}
	return (u.Utime.Sec+u.Stime.Sec)*1e9 + (u.Utime.Usec+u.Stime.Usec)*1e3
}

func TestConnectedReadProfile(t *testing.T) {
	mode := os.Getenv("TCP_READ_PROFILE")
	r := report{Mode: mode, MemProfileRate: runtime.MemProfileRate, Groups: make(map[string]sampled),
		TrimLeaves: make(map[string]sampled), Files: make(map[string]string)}
	writer, reader := qualifiedTCPStream(t)
	warmTCPStream(t, writer, reader, 64)
	data := streamPayload(4 * tcpReadUnit)
	got := make([]byte, len(data))
	var cpu bytes.Buffer
	var pre []byte
	switch mode {
	case "cpu":
		if err := pprof.StartCPUProfile(&cpu); err != nil {
			t.Fatal(err)
		}
	case "alloc":
		if runtime.MemProfileRate != 16384 {
			t.Fatalf("allocation sampling rate %d", runtime.MemProfileRate)
		}
		pre = heapSnapshot(t)
	default:
		t.Fatalf("unknown profile %q", mode)
	}
	var before, after runtime.MemStats
	runtime.ReadMemStats(&before)
	start := processCPU(t)
	for i := 0; i < 64; i++ {
		data[0], data[len(data)-1] = byte(i), byte(i+1)
		r.Reads += int64(transferTCPStream(t, writer, reader, data, got, 64))
		if !bytes.Equal(data, got) {
			t.Fatalf("payload mismatch in batch %d", i)
		}
		r.Bytes += int64(len(got))
	}
	r.ProcessCPUNs = processCPU(t) - start
	runtime.ReadMemStats(&after)
	r.AllocBytes, r.Mallocs = int64(after.TotalAlloc-before.TotalAlloc), int64(after.Mallocs-before.Mallocs)
	if mode == "cpu" {
		pprof.StopCPUProfile()
		readSamples(t, &r, "cpu.pprof", cpu.Bytes(), 1)
	} else {
		post := heapSnapshot(t)
		readSamples(t, &r, "before.pprof", pre, -1)
		readSamples(t, &r, "after.pprof", post, 1)
	}
	checkTCPStream(t, writer, reader, data, got)
	if r.Bytes != 256<<20 || r.Groups["all"].Value <= 0 || (mode == "cpu" && r.Groups["read_total"].Count <= 0) {
		t.Fatalf("incomplete %s profile: %d bytes, groups %v", mode, r.Bytes, r.Groups)
	}
	raw, err := json.Marshal(r)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("CONNECTED_PROFILE %s", raw)
}
