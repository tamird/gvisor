// Copyright 2026 The gVisor Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//	http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
package drivers

import (
	"bytes"
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"
	"testing/iotest"

	"gvisor.dev/gvisor/pkg/sentry/devices/nvproxy"
	"gvisor.dev/gvisor/pkg/sentry/devices/nvproxy/nvconf"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) {
	return f(req)
}

type responseBody struct {
	io.Reader
	closes int
}

func (b *responseBody) Close() error {
	b.closes++
	return nil
}

type downloadWriter struct {
	err error
	bytes.Buffer
	closes int
}

func (w *downloadWriter) Write(p []byte) (int, error) {
	if w.err != nil {
		return 0, w.err
	}
	return w.Buffer.Write(p)
}

func (w *downloadWriter) Close() error {
	w.closes++
	return nil
}

func checksum(data string) string {
	return fmt.Sprintf("%x", sha256.Sum256([]byte(data)))
}

func TestDownload(t *testing.T) {
	const content = "complete driver contents"
	for _, test := range []struct {
		writeErr  error
		wantErr   error
		reader    func() io.Reader
		name      string
		wantBytes string
		status    int
	}{
		{name: "ordinary", reader: func() io.Reader { return strings.NewReader(content) }, status: http.StatusOK, writeErr: nil, wantErr: nil, wantBytes: content},
		{name: "final_data_with_eof", reader: func() io.Reader { return iotest.DataErrReader(strings.NewReader(content)) }, status: http.StatusOK, writeErr: nil, wantErr: nil, wantBytes: content},
		{name: "empty", reader: func() io.Reader { return strings.NewReader("") }, status: http.StatusOK, writeErr: nil, wantErr: nil, wantBytes: ""},
		{name: "read_error", reader: func() io.Reader { return iotest.ErrReader(io.ErrUnexpectedEOF) }, status: http.StatusOK, writeErr: nil, wantErr: io.ErrUnexpectedEOF, wantBytes: ""},
		{name: "partial_read_error", reader: func() io.Reader {
			return io.MultiReader(strings.NewReader(content), iotest.ErrReader(io.ErrUnexpectedEOF))
		}, status: http.StatusOK, writeErr: nil, wantErr: io.ErrUnexpectedEOF, wantBytes: content},
		{name: "write_error", reader: func() io.Reader { return strings.NewReader(content) }, status: http.StatusOK, writeErr: io.ErrClosedPipe, wantErr: io.ErrClosedPipe, wantBytes: ""},
		{name: "not_found", reader: func() io.Reader { return strings.NewReader("missing") }, status: http.StatusNotFound, writeErr: nil, wantErr: nil, wantBytes: ""},
	} {
		t.Run(test.name, func(t *testing.T) {
			body := &responseBody{Reader: test.reader()}
			downloader := Downloader{client: &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
				if got, want := req.Method, http.MethodGet; got != want {
					t.Errorf("method = %q, want %q", got, want)
				}
				return &http.Response{StatusCode: test.status, Status: http.StatusText(test.status), Body: body}, nil
			})}}
			writer := &downloadWriter{err: test.writeErr}
			got, err := downloader.Download(t.Context(), nvconf.NewDriverVersion(1, 2, 3), archAMD64, writer)
			switch {
			case test.status != http.StatusOK:
				if status, ok := errors.AsType[*downloadStatusError](err); !ok || status.code != test.status {
					t.Errorf("Download error = %v, want HTTP %d", err, test.status)
				}
			case test.wantErr != nil:
				if !errors.Is(err, test.wantErr) {
					t.Errorf("Download error = %v, want %v", err, test.wantErr)
				}
			default:
				if err != nil {
					t.Fatalf("Download: %v", err)
				}
				if got, want := got, checksum(test.wantBytes); got != want {
					t.Errorf("checksum = %q, want %q", got, want)
				}
			}
			if err != nil {
				if got, want := got, ""; got != want {
					t.Errorf("failed download checksum = %q, want %q", got, want)
				}
			}
			if got, want := writer.String(), test.wantBytes; got != want {
				t.Errorf("written bytes = %q, want %q", got, want)
			}
			if got, want := body.closes, 1; got != want {
				t.Errorf("response closes = %d, want %d", got, want)
			}
			if got, want := writer.closes, 0; got != want {
				t.Errorf("writer closes = %d, want %d", got, want)
			}
		})
	}
}

func TestValidateChecksum(t *testing.T) {
	const content = "arm64 driver"
	unavailableARM := nvproxy.NewChecksums(nvproxy.ChecksumNoDriver, checksum(content))
	unavailableARM.NoPublicARM64Installer = true
	unavailableARMWithPublicAMD := unavailableARM
	unavailableARMWithPublicAMD.X86_64 = checksum(content)
	unavailableAMD := nvproxy.NewChecksums(checksum(content), checksum(content))
	unavailableAMD.NoPublicX86_64Installer = true
	emptyARM := unavailableARM
	emptyARM.ARM64 = ""
	for _, test := range []struct {
		armError  error
		name      string
		wantErr   string
		checksums nvproxy.Checksums
		amdStatus int
		armStatus int
		requests  int
	}{
		{name: "both_present", checksums: nvproxy.NewChecksums(checksum(content), checksum(content)), amdStatus: http.StatusOK, armStatus: http.StatusOK, wantErr: "", requests: 2, armError: nil},
		{name: "first_missing", checksums: nvproxy.NewChecksums(nvproxy.ChecksumNoDriver, checksum(content)), amdStatus: http.StatusNotFound, armStatus: http.StatusOK, wantErr: "", requests: 2, armError: nil},
		{name: "second_mismatch", checksums: nvproxy.NewChecksums(nvproxy.ChecksumNoDriver, "wrong"), amdStatus: http.StatusNotFound, armStatus: http.StatusOK, wantErr: "checksum mismatch", requests: 2, armError: nil},
		{name: "both_missing", checksums: nvproxy.NewChecksums(nvproxy.ChecksumNoDriver, nvproxy.ChecksumNoDriver), amdStatus: http.StatusNotFound, armStatus: http.StatusNotFound, wantErr: "", requests: 2, armError: nil},
		{name: "missing_is_not_forbidden", checksums: nvproxy.NewChecksums(nvproxy.ChecksumNoDriver, checksum(content)), amdStatus: http.StatusForbidden, armStatus: http.StatusOK, wantErr: "HTTP 403", requests: 1, armError: nil},
		{name: "unexpected_installer", checksums: nvproxy.NewChecksums(nvproxy.ChecksumNoDriver, checksum(content)), amdStatus: http.StatusOK, armStatus: http.StatusOK, wantErr: "unexpected NVIDIA .run installer", requests: 1, armError: nil},
		{name: "unavailable_arm", checksums: unavailableARM, amdStatus: http.StatusNotFound, armStatus: http.StatusNotFound, wantErr: "", requests: 2, armError: nil},
		{name: "unavailable_arm_public_amd", checksums: unavailableARMWithPublicAMD, amdStatus: http.StatusOK, armStatus: http.StatusNotFound, wantErr: "", requests: 2, armError: nil},
		{name: "unavailable_amd_public_arm", checksums: unavailableAMD, amdStatus: http.StatusNotFound, armStatus: http.StatusOK, wantErr: "", requests: 2, armError: nil},
		{name: "unavailable_arm_published", checksums: unavailableARM, amdStatus: http.StatusNotFound, armStatus: http.StatusOK, wantErr: "unexpected NVIDIA .run installer", requests: 2, armError: nil},
		{name: "unavailable_arm_forbidden", checksums: unavailableARM, amdStatus: http.StatusNotFound, armStatus: http.StatusForbidden, wantErr: "HTTP 403", requests: 2, armError: nil},
		{name: "unavailable_arm_transport_error", checksums: unavailableARM, amdStatus: http.StatusNotFound, armStatus: 0, wantErr: "context deadline exceeded", requests: 2, armError: context.DeadlineExceeded},
		{name: "unavailable_arm_empty_checksum", checksums: emptyARM, amdStatus: http.StatusNotFound, armStatus: http.StatusNotFound, wantErr: "empty NVIDIA installer checksum", requests: 0, armError: nil},
	} {
		t.Run(test.name, func(t *testing.T) {
			var bodies []*responseBody
			var requests int
			downloader := Downloader{client: &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
				requests++
				status := test.amdStatus
				if strings.Contains(req.URL.Path, "aarch64") {
					if test.armError != nil {
						return nil, test.armError
					}
					status = test.armStatus
				}
				body := &responseBody{Reader: strings.NewReader(content)}
				bodies = append(bodies, body)
				return &http.Response{StatusCode: status, Body: body}, nil
			})}}
			err := downloader.ValidateChecksum(t.Context(), nvconf.NewDriverVersion(1, 2, 3), test.checksums)
			if test.wantErr == "" {
				if err != nil {
					t.Fatalf("ValidateChecksum: %v", err)
				}
			} else if err == nil || !strings.Contains(err.Error(), test.wantErr) {
				t.Errorf("ValidateChecksum error = %v, want %q", err, test.wantErr)
			}
			if test.armError != nil && !errors.Is(err, test.armError) {
				t.Errorf("ValidateChecksum error = %v, want %v", err, test.armError)
			}
			if got, want := requests, test.requests; got != want {
				t.Errorf("requests = %d, want %d", got, want)
			}
			for _, body := range bodies {
				if got, want := body.closes, 1; got != want {
					t.Errorf("body closes = %d, want %d", got, want)
				}
			}
		})
	}
}
