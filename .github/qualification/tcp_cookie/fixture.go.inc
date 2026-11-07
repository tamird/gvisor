
// A deterministic nonce lets this fixture compare both implementations.
func cookieHashFixture(tb testing.TB, clock tcpip.Clock) *listenContext {
	tb.Helper()
	s := stack.New(stack.Options{Clock: clock})
	tb.Cleanup(s.Destroy)
	ctx := newListenContext(s, nil, nil, 0, false, 0)
	for i := range ctx.nonce {
		for j := range ctx.nonce[i] {
			ctx.nonce[i][j] = byte(128*i + j)
		}
	}
	return ctx
}

func cookieHashFixtureIDs() (stack.TransportEndpointID, stack.TransportEndpointID) {
	v4 := stack.TransportEndpointID{
		LocalPort: 443, RemotePort: 40001,
		LocalAddress:  tcpip.AddrFrom4([4]byte{192, 0, 2, 10}),
		RemoteAddress: tcpip.AddrFrom4([4]byte{198, 51, 100, 20}),
	}
	v6 := stack.TransportEndpointID{
		LocalPort: 1443, RemotePort: 41002,
		LocalAddress:  tcpip.AddrFrom16([16]byte{0: 0x20, 1: 1, 2: 0xd, 3: 0xb8, 15: 10}),
		RemoteAddress: tcpip.AddrFrom16([16]byte{0: 0x20, 1: 1, 2: 0xd, 3: 0xb8, 7: 1, 15: 20}),
	}
	return v4, v6
}

func TestCookieHashCompatibility(t *testing.T) {
	ctx := cookieHashFixture(t, faketime.NewManualClock())
	v4, v6 := cookieHashFixtureIDs()
	mixed := v4
	mixed.RemoteAddress = v6.RemoteAddress
	for _, tc := range []struct {
		name string
		id   stack.TransportEndpointID
		word uint32
		want [2]uint32
	}{
		{"IPv4", v4, 0x01234567, [2]uint32{0xaadb9951, 0xda7e7954}},
		{"IPv6", v6, 0xffffffff, [2]uint32{0xe5165727, 0xcd52423b}},
		{"Mixed", mixed, 0x80000000, [2]uint32{0x03887257, 0x00d66d7e}},
		{"Empty", stack.TransportEndpointID{}, 0, [2]uint32{0x528c2912, 0x94cafb75}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			for i, want := range tc.want {
				if got := ctx.cookieHash(tc.id, tc.word, i); got != want {
					t.Errorf("cookieHash(nonce %d) = %#08x, want %#08x", i, got, want)
				}
			}
		})
	}
}

func TestCookieHashConcurrent(t *testing.T) {
	ctx := cookieHashFixture(t, faketime.NewManualClock())
	v4, v6 := cookieHashFixtureIDs()
	start := make(chan struct{})
	done := make(chan bool, 2)
	for _, id := range []stack.TransportEndpointID{v4, v6} {
		go func() {
			<-start
			for i := 0; i < 512; i++ {
				const seq = 123456
				want := uint32(i % len(mssTable))
				cookie := ctx.createCookie(id, seq, want)
				if got, ok := ctx.isCookieValid(id, cookie, seq); !ok || got != want {
					done <- false
					return
				}
			}
			done <- true
		}()
	}
	close(start)
	for i := 0; i < 2; i++ {
		if !<-done {
			t.Error("creating and validating a cookie on the shared listener failed")
		}
	}
}

func BenchmarkSYNCookieRoundTrip(b *testing.B) {
	ctx := cookieHashFixture(b, nil)
	v4, v6 := cookieHashFixtureIDs()
	for _, tc := range []struct {
		name string
		id   stack.TransportEndpointID
	}{{"IPv4", v4}, {"IPv6", v6}} {
		b.Run(tc.name, func(b *testing.B) {
			const seq = 123456
			const data = 2
			check := func() bool {
				cookie := ctx.createCookie(tc.id, seq, data)
				got, ok := ctx.isCookieValid(tc.id, cookie, seq)
				return ok && got == data
			}
			b.Run("Serial", func(b *testing.B) {
				b.ReportAllocs()
				for b.Loop() {
					if !check() {
						b.Fatal("cookie round trip failed")
					}
				}
			})
			b.Run("Shared", func(b *testing.B) {
				b.ReportAllocs()
				b.RunParallel(func(pb *testing.PB) {
					for pb.Next() {
						if !check() {
							b.Error("shared cookie round trip failed")
							return
						}
					}
				})
			})
		})
	}
}
