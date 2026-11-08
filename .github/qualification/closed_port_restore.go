// This function is appended to the existing TCP migration test on the Linux worker.
func TestClosedPortReservationCheckpoint(t *testing.T) {
	c := testcontext.New(t, e2e.DefaultMTU)
	defer c.Cleanup()

	var q waiter.Queue
	ep, err := c.Stack().NewEndpoint(tcp.ProtocolNumber, ipv4.ProtocolNumber, &q)
	if err != nil {
		t.Fatalf("NewEndpoint: %v", err)
	}
	if err := ep.Bind(tcpip.FullAddress{}); err != nil {
		t.Fatalf("Bind: %v", err)
	}
	bound, err := ep.GetLocalAddress()
	if err != nil || bound.Port == 0 {
		t.Fatalf("GetLocalAddress: %v, %v", bound, err)
	}
	ep.Close()
	if got := tcp.EndpointState(ep.State()); got != tcp.StateClose {
		t.Fatalf("closed endpoint state: got %v, want StateClose", got)
	}

	// The closed endpoint is no longer in the demultiplexer. Keep the
	// application's reference and the source stack in the same graph.
	c.Stack().SetRemoveConf(true)
	roots := []any{c.Stack(), ep}
	var checkpoint bytes.Buffer
	if _, err := state.Save(context.Background(), &checkpoint, &roots); err != nil {
		t.Fatalf("Save: %v", err)
	}
	c.Stack().Resume()
	var loaded []any
	if _, err := state.Load(context.Background(), bytes.NewReader(checkpoint.Bytes()), &loaded); err != nil {
		t.Fatalf("Load: %v", err)
	}
	s := loaded[0].(*stack.Stack)
	defer s.Destroy()
	reopened := loaded[1].(*tcp.Endpoint)
	if got := tcp.EndpointState(reopened.State()); got != tcp.StateInitial {
		t.Fatalf("state after Load: got %v, want StateInitial", got)
	}
	s.Restore()
	if got := tcp.EndpointState(reopened.State()); got != tcp.StateClose {
		t.Fatalf("state after Restore: got %v, want StateClose", got)
	}
	var replacementQueue waiter.Queue
	replacement, err := s.NewEndpoint(tcp.ProtocolNumber, ipv4.ProtocolNumber, &replacementQueue)
	if err != nil {
		t.Fatalf("restored stack NewEndpoint: %v", err)
	}
	defer replacement.Close()
	if err := replacement.Bind(tcpip.FullAddress{Port: bound.Port}); err != nil {
		t.Fatalf("rebind original port %d: %v", bound.Port, err)
	}
	t.Logf("closed-port checkpoint bytes=%d port=%d loaded=Initial restored=Close rebound=true", checkpoint.Len(), bound.Port)
}
