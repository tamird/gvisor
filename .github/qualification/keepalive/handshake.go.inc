
func BenchmarkKeepaliveHandshake(b *testing.B) {
	for _, enabled := range []bool{false, true} {
		b.Run(fmt.Sprintf("enabled=%t", enabled), func(b *testing.B) {
			b.ReportAllocs()
			b.StopTimer()
			for i := 0; i < b.N; i++ {
				n := newTopology(b, false, 0)
				b.StartTimer()
				pair, err := n.connect(n.ctx)
				if err != nil {
					b.Fatal(err)
				}
				if enabled {
					pair.client.ep.SocketOptions().SetKeepAlive(true)
					pair.server.ep.SocketOptions().SetKeepAlive(true)
				}
				b.StopTimer()
				n.close()
			}
		})
	}
}
