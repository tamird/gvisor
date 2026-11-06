
func TestRejectedShutdownReadiness(t *testing.T) {
	var ep Endpoint
	ep.state.Store(uint32(StateInitial))
	if err := ep.Shutdown(tcpip.ShutdownRead); err == nil {
		t.Fatal("shutdown succeeded on an unconnected endpoint")
	} else if _, ok := err.(*tcpip.ErrNotConnected); !ok {
		t.Fatalf("shutdown error = %T, want ErrNotConnected", err)
	}
	if got := ep.saveShutdownFlags(); got != tcpip.ShutdownRead {
		t.Fatalf("saved shutdown flags = %d, want ShutdownRead", got)
	}
	var restored Endpoint
	restored.loadShutdownFlags(context.Background(), ep.saveShutdownFlags())
	for _, e := range []*Endpoint{&ep, &restored} {
		e.state.Store(uint32(StateEstablished))
		if got := e.Readiness(waiter.EventRdHUp); got != 0 {
			t.Fatalf("Readiness(EventRdHUp) = %v after rejected shutdown", got)
		}
	}
}
