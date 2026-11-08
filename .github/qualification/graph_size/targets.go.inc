func BenchmarkQualificationTargets(b *testing.B) {
	previous := state.GraphEngineEnabled()
	state.EnableGraphEngine(true)
	defer state.EnableGraphEngine(previous)

	for _, shape := range []string{"Ordinary", "ZeroSized"} {
		b.Run(shape, func(b *testing.B) {
			const count = 4096
			parents := make([]zeroSizedFields, count)
			roots := make([]system, count)
			for i := range roots {
				p := &parents[i]
				p.value = int64(7*i + 19)
				var child any = &p.value
				if shape == "ZeroSized" {
					child = &p.leading
				}
				roots[i] = system{v1: child, v2: p}
				if i%2 != 0 {
					roots[i].v1, roots[i].v2 = roots[i].v2, roots[i].v1
				}
			}

			var encoded bytes.Buffer
			var restored []system
			b.ReportAllocs()
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				encoded.Reset()
				if _, err := state.Save(b.Context(), &encoded, &roots); err != nil {
					b.Fatal(err)
				}
				restored = nil
				if _, err := state.Load(b.Context(), bytes.NewReader(encoded.Bytes()), &restored); err != nil {
					b.Fatal(err)
				}
			}
			b.StopTimer()
			if len(restored) != count {
				b.Fatalf("got %d parents, want %d", len(restored), count)
			}
			for i, pair := range restored {
				if i%2 != 0 {
					pair.v1, pair.v2 = pair.v2, pair.v1
				}
				p := pair.v2.(*zeroSizedFields)
				var want any = &p.value
				if shape == "ZeroSized" {
					want = &p.leading
				}
				if pair.v1 != want || p.value != int64(7*i+19) {
					b.Fatalf("lost pointer identity or payload for parent %d", i)
				}
			}
			b.ReportMetric(count, "parents/op")
			b.Logf("TARGET_GRAPH %s %d %x", shape, encoded.Len(), sha256.Sum256(encoded.Bytes()))
		})
	}
}
