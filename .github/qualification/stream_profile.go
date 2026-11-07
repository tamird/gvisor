package gonet

import (
	"encoding/json"
	"fmt"
	"testing"

	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
)

func TestConnectedTCPReadShapes(t *testing.T) {
	for _, size := range []int{64, 64 << 10} {
		t.Run(fmt.Sprintf("read=%d", size), func(t *testing.T) {
			writer, reader := qualifiedTCPStream(t)
			warmTCPStream(t, writer, reader, size)
			data := streamPayload(4 * tcpReadUnit)
			got := make([]byte, len(data))
			views := map[int]int{}
			headers := map[int]int{}
			payloads := map[int]int{}
			saw, partial, complete := 0, 0, 0
			tcp.QualificationObserveTCPRead = func(length, header, count, used int, first bool) {
				if first {
					saw += length
					views[count]++
					headers[header]++
					payloads[length]++
				}
				if used < length {
					partial++
				} else {
					complete++
				}
			}
			defer func() { tcp.QualificationObserveTCPRead = nil }()
			reads := transferTCPStream(t, writer, reader, data, got, size)
			checkTCPStream(t, writer, reader, data, got)
			if saw != len(data) || len(views) == 0 {
				t.Fatalf("first touches cover %d of %d bytes: %v", saw, len(data), views)
			}
			row := map[string]any{"read": size, "bytes": len(data), "calls": reads, "partial": partial, "complete": complete, "payload_views": views, "header_bytes": headers, "payload_bytes": payloads}
			line, err := json.Marshal(row)
			if err != nil {
				t.Fatal(err)
			}
			t.Logf("TCP_STREAM_SHAPE %s", line)
		})
	}
}
