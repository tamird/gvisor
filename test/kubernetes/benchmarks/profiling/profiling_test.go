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

package profiling

import (
	"errors"
	"io"
	"strings"
	"testing"
)

func TestDirStreamReaderTerminalResult(t *testing.T) {
	readErr := errors.New("read failure")
	pr, pw := io.Pipe()
	defer pr.Close()
	if err := pw.CloseWithError(readErr); err != nil {
		t.Fatal(err)
	}

	for _, test := range []struct {
		reader   io.ReadCloser
		wantErr  error
		name     string
		wantText string
	}{
		{name: "EOF", reader: io.NopCloser(strings.NewReader("")), wantErr: io.EOF},
		{name: "read error", reader: pr, wantErr: readErr},
		{name: "invalid line", reader: io.NopCloser(strings.NewReader("invalid\n")), wantText: "invalid line format"},
	} {
		t.Run(test.name, func(t *testing.T) {
			r := &dirStreamReader{
				logsReader: test.reader,
				dataCh:     make(chan []byte),
				errCh:      make(chan error),
			}
			done := make(chan struct{})
			go func() {
				r.processLogs()
				close(done)
			}()
			var results []error
		collect:
			for {
				select {
				case err := <-r.errCh:
					results = append(results, err)
				case <-done:
					break collect
				}
			}
			if len(results) != 1 {
				t.Fatalf("terminal results = %v, want exactly one", results)
			}
			if test.wantErr != nil && !errors.Is(results[0], test.wantErr) {
				t.Errorf("terminal error = %v, want %v", results[0], test.wantErr)
			}
			if test.wantText != "" && (results[0] == nil || !strings.Contains(results[0].Error(), test.wantText)) {
				t.Errorf("terminal error = %v, want text %q", results[0], test.wantText)
			}
		})
	}
}
