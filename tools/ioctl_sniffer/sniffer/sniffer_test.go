// Copyright 2026 The gVisor Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package sniffer

import (
	"context"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"syscall"
	"testing"

	"google.golang.org/protobuf/proto"
	"gvisor.dev/gvisor/pkg/abi/linux"
	"gvisor.dev/gvisor/pkg/abi/nvgpu"
	pb "gvisor.dev/gvisor/tools/ioctl_sniffer/ioctl_go_proto"
)

func encodeFrame(body []byte) []byte {
	encoded := make([]byte, 8, 8+len(body))
	binary.LittleEndian.PutUint64(encoded, uint64(len(body)))
	return append(encoded, body...)
}

func marshalFrame(t *testing.T, message *pb.Ioctl) []byte {
	t.Helper()
	body, err := proto.Marshal(message)
	if err != nil {
		t.Fatal(err)
	}
	return encodeFrame(body)
}

func TestFailedReadableControl(t *testing.T) {
	params := nvgpu.NVOS54_PARAMETERS{Cmd: 0x1234, Status: nvgpu.NV_ERR_NOT_SUPPORTED}
	data := make([]byte, nvgpu.SizeofNVOS54Parameters)
	params.MarshalBytes(data)
	parsed, err := ParseIoctlOutput(&pb.Ioctl{
		FdPath:  "/dev/nvidiactl",
		Request: uint64(linux.IOWR('F', nvgpu.NV_ESC_RM_CONTROL, nvgpu.SizeofNVOS54Parameters)),
		Ret:     -1,
		ArgData: data,
	})
	if err != nil {
		t.Fatal(err)
	}
	if got, want := parsed.class, control; got != want {
		t.Errorf("class = %v, want %v", got, want)
	}
	if got, want := parsed.subclass, ioctlSubclass(params.Cmd); got != want {
		t.Errorf("subclass = %#x, want %#x", got, want)
	}
	if got, want := parsed.status, params.Status; got != want {
		t.Errorf("status = %#x, want %#x", got, want)
	}
	if got, want := parsed.IsSupported(), true; got != want {
		t.Errorf("IsSupported = %v, want %v for NV_ERR_NOT_SUPPORTED", got, want)
	}
}

func TestIncompleteArguments(t *testing.T) {
	for _, tc := range []struct {
		name  string
		data  []byte
		errno int32
	}{
		{name: "fault", errno: int32(syscall.EFAULT)},
		{name: "denied", errno: int32(syscall.EPERM)},
		{name: "partial", data: make([]byte, 16)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, err := ParseIoctlOutput(&pb.Ioctl{
				FdPath: "/dev/nvidia0", Request: uint64(linux.IOWR('F', 0x20, 32)),
				Ret: -1, ArgData: tc.data, ArgDataErrno: tc.errno,
			})
			if err == nil {
				t.Fatal("ParseIoctlOutput succeeded, want incomplete capture error")
			}
			if tc.errno != 0 && !errors.Is(err, syscall.Errno(tc.errno)) {
				t.Errorf("error = %v, want errno %d", err, tc.errno)
			}
		})
	}
}

func TestReadHookOutput(t *testing.T) {
	captureError := marshalFrame(t, &pb.Ioctl{FdPath: "/dev/nvidia0", Ret: -1, ArgDataErrno: int32(syscall.EFAULT)})
	unsupported := marshalFrame(t, &pb.Ioctl{FdPath: "/dev/nvidia0", Request: uint64(linux.IOWR('F', 0xfd, 0)), Ret: -1})
	payload := encodeFrame([]byte{1, 2, 3, 4})
	for _, tc := range []struct {
		name        string
		input       []byte
		wantError   bool
		err         error
		unsupported bool
	}{
		{name: "clean_eof"},
		{name: "empty_proto", input: encodeFrame(nil), unsupported: true},
		{name: "partial_header", input: []byte{1, 2, 3}, wantError: true, err: io.ErrUnexpectedEOF},
		{name: "missing_payload", input: payload[:8], wantError: true, err: io.ErrUnexpectedEOF},
		{name: "partial_payload", input: payload[:10], wantError: true, err: io.ErrUnexpectedEOF},
		{name: "malformed_proto", input: encodeFrame([]byte{0xff}), wantError: true},
		{name: "capture_error_then_valid", input: append(captureError, unsupported...), wantError: true, err: syscall.EFAULT, unsupported: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			reader, writer := net.Pipe()
			defer reader.Close()
			defer writer.Close()
			written := make(chan error, 1)
			go func() {
				var err error
				if len(tc.input) != 0 {
					_, err = writer.Write(tc.input)
				}
				written <- errors.Join(err, writer.Close())
			}()
			connection := Connection{conn: reader}
			results, err := connection.ReadHookOutput(t.Context())
			if got, want := err != nil, tc.wantError; got != want {
				t.Errorf("ReadHookOutput error = %v, want error=%v", err, want)
			}
			if tc.err != nil && !errors.Is(err, tc.err) {
				t.Errorf("ReadHookOutput error = %v, want %v", err, tc.err)
			}
			if got, want := results.HasUnsupportedIoctl(), tc.unsupported; got != want {
				t.Errorf("unsupported = %v, want %v", got, want)
			}
			if err := <-written; err != nil {
				t.Errorf("writing fixture: %v", err)
			}
		})
	}
}

func TestServerCollectsResultsAndErrors(t *testing.T) {
	server := NewServer()
	if err := server.Listen(); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	served := make(chan error, 1)
	go func() { served <- server.Serve(ctx) }()
	t.Cleanup(func() {
		cancel()
		if err := <-served; err != nil {
			t.Errorf("Serve: %v", err)
		}
		results, err := server.AllResults()
		if !errors.Is(err, syscall.EFAULT) {
			t.Errorf("capture error = %v, want EFAULT", err)
		}
		if got, want := results.HasUnsupportedIoctl(), true; got != want {
			t.Errorf("unsupported capture retained = %v, want %v", got, want)
		}
	})
	for _, message := range []*pb.Ioctl{
		{FdPath: "/dev/nvidia0", Ret: -1, ArgDataErrno: int32(syscall.EFAULT)},
		{FdPath: "/dev/nvidia0", Request: uint64(linux.IOWR('F', 0xfd, 0)), Ret: -1},
	} {
		connection, err := net.Dial("unix", server.Addr())
		if err != nil {
			t.Fatal(err)
		}
		defer connection.Close()
		if _, err := connection.Write(marshalFrame(t, message)); err != nil {
			t.Fatal(err)
		}
		if err := connection.(*net.UnixConn).CloseWrite(); err != nil {
			t.Fatal(err)
		}
		// Reader ownership ends before the result channel is drained. EOF is
		// also a deterministic acknowledgement that this connection was read.
		var trailing [1]byte
		n, err := connection.Read(trailing[:])
		if got, want := n, 0; got != want || !errors.Is(err, io.EOF) {
			t.Fatalf("read after frame = (%d, %v), want (%d, EOF)", got, err, want)
		}
	}
}
