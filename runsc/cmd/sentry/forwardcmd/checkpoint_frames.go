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

package forwardcmd

import (
	"bufio"
	"bytes"
	"fmt"
	"io"

	"gvisor.dev/gvisor/pkg/sentry/pgalloc/metadata"
	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/state/wire"
)

// checkpointFrames mirrors kernel.Kernel.SaveTo: the CPU and kernel object
// graphs are followed, for inline saves, by the application MemoryFile, the
// private MemoryFile owners graph, and each private MemoryFile in turn.
type checkpointFrames struct {
	r         *bufio.Reader
	objects   int
	legacy    bool
	haveApp   bool
	pageSizes []uint64
}

func (s *checkpointFrames) framed() (state.Header, io.Reader, error) {
	h, err := state.ReadHeaderMode(&wire.Reader{Reader: s.r})
	return h, s.r, err
}

func (s *checkpointFrames) next() (state.Header, io.Reader, error) {
	if s.legacy {
		return s.framed()
	}
	if len(s.pageSizes) != 0 {
		want := s.pageSizes[0]
		s.pageSizes = s.pageSizes[1:]
		h, r, err := s.framed()
		if err == io.EOF {
			err = io.ErrUnexpectedEOF
		}
		if err == nil && (h.Object || h.Length != want) {
			err = fmt.Errorf("MemoryFile pages: got header %+v, want %d raw bytes", h, want)
		}
		return h, r, err
	}
	if s.objects != 0 {
		s.objects--
		h, r, err := s.framed()
		if err == io.EOF {
			err = io.ErrUnexpectedEOF
		}
		if err == nil && !h.Object {
			err = fmt.Errorf("expected checkpoint object graph")
		}
		return h, r, err
	}

	// A split checkpoint ends after the initial graphs. Inline images
	// written before the protobuf conversion start with a state object
	// here. Its 10-byte varint has the continuation bit set on byte 8.
	// A valid little-endian MemoryFile length fits in int64 and cannot.
	prefix, err := s.r.Peek(8)
	if err != nil {
		if err == io.EOF && len(prefix) != 0 {
			err = io.ErrUnexpectedEOF
		}
		return state.Header{}, nil, err
	}
	if !s.haveApp && prefix[7]&0x80 != 0 {
		for _, b := range prefix {
			if b&0x80 == 0 {
				return state.Header{}, nil, fmt.Errorf("invalid MemoryFile metadata length")
			}
		}
		s.legacy = true
		return s.framed()
	}
	length, err := metadata.ReadLength(s.r)
	if err != nil {
		return state.Header{}, nil, err
	}
	// Grow with the input instead of allocating the advertised length
	// before confirming that the file contains those bytes.
	data, err := io.ReadAll(io.LimitReader(s.r, length))
	if err != nil {
		return state.Header{}, nil, err
	}
	if int64(len(data)) != length {
		return state.Header{}, nil, io.ErrUnexpectedEOF
	}
	pb, err := metadata.Decode(data)
	if err != nil {
		return state.Header{}, nil, err
	}
	for _, segment := range pb.MemAcct {
		if segment.KnownCommitted {
			if segment.End <= segment.Start {
				return state.Header{}, nil, fmt.Errorf("invalid committed MemoryFile range [%d,%d)", segment.Start, segment.End)
			}
			s.pageSizes = append(s.pageSizes, segment.End-segment.Start)
		}
	}
	if !s.haveApp {
		s.haveApp = true
		s.objects = 1 // The private MemoryFile owners graph follows the application pages.
	}
	return state.Header{Length: uint64(len(data))}, bytes.NewReader(data), nil
}
