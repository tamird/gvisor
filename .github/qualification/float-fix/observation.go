
func TestCheckpointFloatBitsObservation(t *testing.T) {
	for _, engine := range []string{"legacy", "graph"} {
		roundTrip := func(src, dst any) {
			t.Helper()
			var buf bytes.Buffer
			if engine == "legacy" {
				if _, err := Save(t.Context(), &buf, src); err != nil {
					t.Fatal(err)
				}
				if _, err := Load(t.Context(), &buf, dst); err != nil {
					t.Fatal(err)
				}
				return
			}
			if _, err := saveGraph(t.Context(), &buf, src); err != nil {
				t.Fatal(err)
			}
			wr := wire.Reader{Reader: &buf}
			hdr, err := ReadHeaderMode(&wr)
			if err != nil || !hdr.Graph || !hdr.Object {
				t.Fatalf("header=%+v, err=%v", hdr, err)
			}
			if _, err := loadGraph(t.Context(), &buf, dst, hdr.Length, 0); err != nil {
				t.Fatal(err)
			}
		}
		for _, bits := range []uint32{0, 0x80000000, 0xc1200000, 0x7fc01234, 0x7f801234, 0xff801234} {
			x := math.Float32frombits(bits)
			var scalar float32
			roundTrip(&x, &scalar)
			t.Logf("BITS %s scalar %08x %08x", engine, math.Float32bits(x), math.Float32bits(scalar))
			z := complex(float32(1), x)
			var scalarComplex complex64
			roundTrip(&z, &scalarComplex)
			t.Logf("BITS %s complex %08x %08x", engine, math.Float32bits(imag(z)), math.Float32bits(imag(scalarComplex)))
			v := []float32{x, 1}
			v = v[:1]
			var slice []float32
			roundTrip(&v, &slice)
			if len(slice) != 1 || cap(slice) != 2 {
				t.Fatalf("slice restored as len=%d cap=%d", len(slice), cap(slice))
			}
			t.Logf("BITS %s slice %08x %08x", engine, math.Float32bits(v[0]), math.Float32bits(slice[0]))
		}
	}
}
