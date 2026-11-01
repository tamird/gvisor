
// TestBurstGoodput measures an application that reads only when the peer has
// exhausted its advertised window or needs to recover a missing segment.
func TestBurstGoodput(t *testing.T) {
	for _, mss := range []int{100, 536, 1448} {
		t.Run(fmt.Sprint(mss), func(t *testing.T) {
			c := context.New(t, e2e.DefaultMTU)
			defer c.Cleanup()
			c.CreateConnectedWithRawOptions(context.TestInitialSequenceNumber, 30000, 32<<10, []byte{
				header.TCPOptionWS, header.TCPOptionWSLength, 0, header.TCPOptionNOP,
			})
			// Use the 4 KiB send budget from the control-backlog reproducer
			// while leaving the receive buffer at the benchmark’s 64 KiB.
			c.EP.SocketOptions().SetSendBufferSize(4096, true)
			const size = 1 << 20
			data := make([]byte, size)
			for i := range data {
				data[i] = byte(i%251)
			}
			var received bytes.Buffer
			received.Grow(size)
			read := func() {
				for {
					result, err := c.EP.Read(&received, tcpip.ReadOptions{})
					if _, ok := err.(*tcpip.ErrWouldBlock); ok {
						return
					}
					if err != nil {
						t.Fatalf("Read: %s", err)
					}
					if result.Count == 0 {
						t.Fatal("successful read made no progress")
					}
				}
			}
			startSeq := seqnum.Value(context.TestInitialSequenceNumber).Add(1)
			send := func(pos, n int) {
				c.SendPacket(data[pos:pos+n], &context.Headers{
					SrcPort: context.TestPort, DstPort: c.Port,
					SeqNum: startSeq.Add(seqnum.Size(pos)), AckNum: c.IRS.Add(1),
					Flags: header.TCPFlagAck, RcvWnd: 30000,
				})
			}
			// Prime the receiver with one byte to learn its advertised right edge.
			send(0, 1)
			first := c.GetPacket()
			h := header.TCP(header.IPv4(first.AsSlice()).Payload())
			if got, want := h.AckNumber(), uint32(startSeq)+1; got != want {
				first.Release()
				t.Fatalf("first ACK = %d, want %d", got, want)
			}
			acked, high, next := 1, 1, 1
			edge := acked + (int(h.WindowSize()) << c.RcvdWindowScale)
			first.Release()
			dups, silence, retransmits, zeroWindows := 0, 0, 0, 0
			consume := func(packet *buffer.View) {
				defer packet.Release()
				h := header.TCP(header.IPv4(packet.AsSlice()).Payload())
				pos := int(h.AckNumber() - uint32(startSeq))
				if pos > high {
					t.Fatalf("ACK %d beyond highest transmitted byte %d", pos, high)
				}
				if pos < acked {
					return
				}
				newEdge := pos + (int(h.WindowSize()) << c.RcvdWindowScale)
				if pos == acked && newEdge == edge && pos < high {
					dups++
				} else {
					dups = 0
				}
				if newEdge == pos && edge != acked {
					zeroWindows++
				}
				acked, edge = pos, newEdge
				next = max(next, acked)
			}
			start := time.Now()
			for acked < size {
				if time.Since(start) > 40*time.Second {
					t.Fatalf("no completion: ACK %d, next %d, window edge %d, delivered %d", acked, next, edge, received.Len())
				}
				// Drain the observation channel between bursts so its 1,000
				// slots don't become the bottleneck under test.
				for i := 0; i < 64 && next < min(edge, size); i++ {
					n := min(mss, min(edge, size)-next)
					send(next, n)
					next += n
					high = max(high, next)
				}
				observed := false
				for p := c.GetPacketNonBlocking(); p != nil; p = c.GetPacketNonBlocking() {
					consume(p)
					observed = true
				}
				if dups >= 3 {
					read()
					next, dups = acked, 0
					retransmits++
					continue
				}
				if edge == acked {
					read()
				}
				if observed || next < min(edge, size) || acked == size {
					continue
				}
				if p := c.GetPacketWithTimeout(200*time.Millisecond); p != nil {
					consume(p)
				} else {
					silence++
					read()
					next = acked
					if edge == acked {
						send(acked-1, 1)
					}
				}
			}
			read()
			elapsed := time.Since(start)
			if !bytes.Equal(received.Bytes(), data) {
				t.Fatalf("received %d bytes, expected the complete %d-byte pattern", received.Len(), size)
			}
			drops := c.EP.Stats().(*tcp.Stats).ReceiveErrors.SegmentQueueDropped.Value()
			t.Logf("GOODPUT mss=%d bytes=%d ns=%d dropped=%d silence=%d recovery=%d zero_windows=%d",
				mss, size-1, elapsed.Nanoseconds(), drops, silence, retransmits, zeroWindows)
		})
	}
}
