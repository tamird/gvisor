
// BenchmarkFDAttachCycle measures one complete endpoint lifetime, with empty
// receive queues. The caller owns the socket pairs and reuses them between
// iterations. Socketpair creation is outside the timed region.
func BenchmarkFDAttachCycle(b *testing.B) {
	for _, channels := range []int{1, 4} {
		for _, processors := range []int{1, 2} {
			b.Run(fmt.Sprintf("fds=%d/processors=%d", channels, processors), func(b *testing.B) {
				var fds []int
				for range channels {
					pair, err := unix.Socketpair(unix.AF_UNIX, unix.SOCK_SEQPACKET, 0)
					if err != nil {
						b.Fatal(err)
					}
					fds = append(fds, pair[0])
					b.Cleanup(func() {
						unix.Close(pair[0])
						unix.Close(pair[1])
					})
				}
				opts := Options{FDs: fds, MTU: 1500, ProcessorsPerChannel: processors, PacketDispatchMode: RecvMMsg}
				sink := &fakeNetworkDispatcher{}
				b.ReportAllocs()
				b.ResetTimer()
				for i := 0; i < b.N; i++ {
					ep, err := New(&opts)
					if err != nil {
						b.Fatal(err)
					}
					ep.Attach(sink)
					ep.Attach(nil)
					ep.Close()
				}
			})
		}
	}
}
