package main

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"sort"
	"syscall"
	"time"
)

type span struct {
	seq  uint32
	size int
}
type innerCapture struct {
	Packets        int
	Bytes          int
	LargestIP      int
	BadIPChecksum  int
	BadTCPChecksum int
	Error          string
}

func csum(data []byte) int {
	s := 0
	for len(data) > 1 {
		s += int(binary.BigEndian.Uint16(data))
		data = data[2:]
	}
	if len(data) != 0 {
		s += int(data[0]) << 8
	}
	return s
}
func fold(s int) int {
	for s > 65535 {
		s = (s & 65535) + (s >> 16)
	}
	return s
}

func captureInner(idx int) func() innerCapture {
	const ipv4 = 0x0008 // htons(ETH_P_IP) on Linux/AMD64
	fd, err := syscall.Socket(syscall.AF_PACKET, syscall.SOCK_DGRAM|syscall.SOCK_NONBLOCK, ipv4)
	if err != nil {
		return func() innerCapture { return innerCapture{Error: err.Error()} }
	}
	if err = syscall.Bind(fd, &syscall.SockaddrLinklayer{Protocol: ipv4, Ifindex: idx}); err != nil {
		syscall.Close(fd)
		return func() innerCapture { return innerCapture{Error: err.Error()} }
	}
	if err := syscall.SetsockoptInt(fd, syscall.SOL_SOCKET, syscall.SO_RCVBUF, 8<<20); err != nil {
		syscall.Close(fd)
		return func() innerCapture { return innerCapture{Error: err.Error()} }
	}
	stopped := make(chan struct{})
	done := make(chan innerCapture, 1)
	go func() {
		defer syscall.Close(fd)
		var result innerCapture
		var spans []span
		buf := make([]byte, 65536)
		src, dst := []byte{192, 0, 2, 1}, []byte{198, 51, 100, 2}
		for {
			n, _, err := syscall.Recvfrom(fd, buf, 0)
			if err == syscall.EINTR {
				continue
			}
			if err == syscall.EAGAIN {
				select {
				case <-stopped:
					goto finished
				default:
					time.Sleep(time.Millisecond)
					continue
				}
			}
			if err != nil {
				result.Error = fmt.Sprint(err)
				break
			}
			ip := buf[:n]
			if len(ip) > 14 && ip[0]>>4 != 4 && binary.BigEndian.Uint16(ip[12:14]) == 0x0800 {
				ip = ip[14:]
			}
			if len(ip) < 40 || ip[0]>>4 != 4 || ip[9] != 6 || !bytes.Equal(ip[12:16], src) || !bytes.Equal(ip[16:20], dst) {
				continue
			}
			ih := int(ip[0]&15) * 4
			size := int(binary.BigEndian.Uint16(ip[2:4]))
			if ih < 20 || size > len(ip) || size < ih+20 {
				continue
			}
			tcp := ip[ih:size]
			if binary.BigEndian.Uint16(tcp) != 14011 {
				continue
			}
			th := int(tcp[12]>>4) * 4
			if th < 20 || len(tcp) <= th {
				continue
			}
			if fold(csum(ip[:ih])) != 65535 {
				result.BadIPChecksum++
			}
			if fold(csum(ip[12:20])+6+len(tcp)+csum(tcp)) != 65535 {
				result.BadTCPChecksum++
			}
			result.Packets++
			if size > result.LargestIP {
				result.LargestIP = size
			}
			spans = append(spans, span{binary.BigEndian.Uint32(tcp[4:8]), len(tcp) - th})
		}
	finished:
		if len(spans) > 0 {
			first := spans[0].seq
			sort.Slice(spans, func(i, j int) bool { return int32(spans[i].seq-first) < int32(spans[j].seq-first) })
			end := int(int32(spans[0].seq - first))
			for _, span := range spans {
				start := int(int32(span.seq - first))
				right := start + span.size
				if right > end {
					if start > end {
						end = start
					}
					result.Bytes += right - end
					end = right
				}
			}
		}
		done <- result
	}()
	return func() innerCapture { close(stopped); return <-done }
}
