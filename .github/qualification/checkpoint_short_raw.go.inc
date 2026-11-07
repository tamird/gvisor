
func TestHistoricalShortRawFrame(t *testing.T) {
	var stream bytes.Buffer
	w := wire.Writer{Writer: &stream}
	if err := state.WriteHeader(&w, 11, false); err != nil {
		t.Fatal(err)
	}
	stream.WriteString("abc")
	for _, tc := range []struct {
		name string
		print func(io.Writer, io.Reader) error
		printed, empty string
	}{
		{"text", PrintText, "(11 bytes non-object data)\n", ""},
		{"html", PrintHTML, "<pre>(11 bytes non-object data)\n</pre>", "<pre></pre>"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var out bytes.Buffer
			err := tc.print(&out, bytes.NewReader(stream.Bytes()))
			var result string
			switch {
			case err == nil && out.String() == tc.printed:
				result = "original"
			case errors.Is(err, io.ErrUnexpectedEOF) && out.String() == tc.empty:
				result = "strict"
			default:
				t.Fatalf("unclassified short-frame result: %v, %q", err, out.String())
			}
			t.Logf("SHORT_RAW mode=%s result=%s", tc.name, result)
		})
	}
}
