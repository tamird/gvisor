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

// Package metadata reads the length-prefixed protobuf saved for a MemoryFile.
package metadata

import (
	"encoding/binary"
	"fmt"
	"io"
	"math"

	"google.golang.org/protobuf/proto"
	pgallocpb "gvisor.dev/gvisor/pkg/sentry/pgalloc/pgalloc_metadata_go_proto"
)

// ReadLength reads the byte length preceding the protobuf.
func ReadLength(r io.Reader) (int64, error) {
	var prefix [8]byte
	if _, err := io.ReadFull(r, prefix[:]); err != nil {
		return 0, fmt.Errorf("failed to read metadata length: %w", err)
	}
	length := binary.LittleEndian.Uint64(prefix[:])
	if length > math.MaxInt64 {
		return 0, fmt.Errorf("invalid MemoryFile metadata length %d", length)
	}
	return int64(length), nil
}

// Decode unmarshals the saved version of the metadata.
func Decode(data []byte) (*pgallocpb.MemoryFileMetadataProto, error) {
	var pb pgallocpb.MemoryFileMetadataProto
	if err := proto.Unmarshal(data, &pb); err != nil {
		return nil, fmt.Errorf("failed to unmarshal metadata: %w", err)
	}
	if pb.Version != 1 {
		return nil, fmt.Errorf("unsupported MemoryFileMetadataProto version %d", pb.Version)
	}
	return &pb, nil
}
