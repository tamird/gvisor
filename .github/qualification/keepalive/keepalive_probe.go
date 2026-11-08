// loadKeepaliveProbeStack loads the saved stack before endpoint restoration.
// A nonzero linkMTU recreates NIC 1 at the original IP address.
func loadKeepaliveProbeStack(t *testing.T, image []byte, allowMigration bool, linkMTU uint32) (*stack.Stack, *channel.Endpoint) {
	t.Helper()
	s := stack.New(stack.Options{
		TransportProtocols: []stack.TransportProtocolFactory{tcp.NewProtocol},
		NetworkProtocols:   []stack.NetworkProtocolFactory{ipv4.NewProtocol, ipv6.NewProtocol},
	})
	t.Cleanup(s.Destroy)
	s.SetAllowLiveTCPMigration(allowMigration)
	loadStats, err := state.Load(context.Background(), bytes.NewReader(image), s)
	if err != nil {
		t.Fatalf("Load failed: %v", err)
	}
	t.Logf("Load stats:\n%s", loadStats.String())
	if linkMTU == 0 {
		return s, nil
	}
	link := channel.New(1000, linkMTU, "")
	if err := s.CreateNIC(1, link); err != nil {
		t.Fatalf("CreateNIC failed: %v", err)
	}
	if err := s.AddProtocolAddress(1, tcpip.ProtocolAddress{
		Protocol:          header.IPv4ProtocolNumber,
		AddressWithPrefix: testcontext.StackAddrWithPrefix,
	}, stack.AddressProperties{}); err != nil {
		t.Fatalf("AddProtocolAddress failed: %v", err)
	}
	s.SetRouteTable([]tcpip.Route{{Destination: header.IPv4EmptySubnet, NIC: 1}})
	return s, link
}

// TestKeepaliveAllocationWire observes the same sources on both sides of the allocation change.
func TestKeepaliveAllocationWire(t *testing.T) {
	const idle = 2 * time.Second
	newFlow := func(t *testing.T) (*testcontext.Context, *faketime.ManualClock) {
		t.Helper()
		clock := faketime.NewManualClock()
		c := testcontext.NewWithOpts(t, testcontext.Options{EnableV4: true, EnableV6: true, MTU: e2e.DefaultMTU, Clock: clock})
		t.Cleanup(c.Cleanup)
		return c, clock
	}
	rearm := func(t *testing.T, ep tcpip.Endpoint) {
		t.Helper()
		v := tcpip.KeepaliveIdleOption(idle)
		if err := ep.SetSockOpt(&v); err != nil {
			t.Fatalf("set keepalive idle: %v", err)
		}
	}
	configure := func(t *testing.T, ep tcpip.Endpoint) {
		t.Helper()
		rearm(t, ep)
		interval := tcpip.KeepaliveIntervalOption(3 * idle)
		if err := ep.SetSockOpt(&interval); err != nil {
			t.Fatal(err)
		}
		if err := ep.SetSockOptInt(tcpip.KeepaliveCountOption, 4); err != nil {
			t.Fatal(err)
		}
		ep.SocketOptions().SetKeepAlive(true)
	}
	// A keepalive on this idle flow has no user bytes, sequence SND.NXT-1
	// (the Sentry's initial sequence number), and acknowledges the peer's SYN.
	probe := func(t *testing.T, read func() *buffer.View, localPort uint16, iss seqnum.Value) bool {
		t.Helper()
		v := read()
		if v == nil {
			return false
		}
		defer v.Release()
		ip := header.IPv4(v.AsSlice())
		if len(ip) < header.IPv4MinimumSize || ip.SourceAddress() != testcontext.StackAddr || ip.DestinationAddress() != testcontext.TestAddr || ip.TransportProtocol() != tcp.ProtocolNumber {
			t.Fatalf("unexpected IPv4 probe header: %x", v.AsSlice())
		}
		h := header.TCP(ip.Payload())
		if len(h) < header.TCPMinimumSize || h.SourcePort() != localPort || h.DestinationPort() != testcontext.TestPort ||
			h.Flags() != header.TCPFlagAck || h.SequenceNumber() != uint32(iss) ||
			h.AckNumber() != testcontext.TestInitialSequenceNumber+1 || int(h.DataOffset()) != len(h) {
			t.Fatalf("unexpected keepalive packet: %x", v.AsSlice())
		}
		return true
	}
	deadline := func(t *testing.T, clock *faketime.ManualClock, read func() *buffer.View, port uint16, iss seqnum.Value) bool {
		t.Helper()
		clock.Advance(idle - time.Nanosecond)
		if probe(t, read, port, iss) {
			t.Fatal("keepalive before the idle deadline")
		}
		clock.Advance(time.Nanosecond)
		got := probe(t, read, port, iss)
		if probe(t, read, port, iss) {
			t.Fatal("multiple keepalives at the same deadline")
		}
		return got
	}

	t.Run("BeforeConnect", func(t *testing.T) {
		c, clock := newFlow(t)
		c.Create(-1)
		configure(t, c.EP)
		clock.Advance(idle)
		if v := c.GetPacketNonBlocking(); v != nil {
			v.Release()
			t.Fatal("unconnected socket sent a packet")
		}
		c.Connect(testcontext.TestInitialSequenceNumber, 30000, nil)
		got := deadline(t, clock, c.GetPacketNonBlocking, c.Port, c.IRS)
		t.Logf("KEEPALIVE_WIRE before_connect_deadline=%t", got)
	})

	t.Run("DisableReenable", func(t *testing.T) {
		c, clock := newFlow(t)
		c.CreateConnected(testcontext.TestInitialSequenceNumber, 30000, -1)
		configure(t, c.EP)
		clock.Advance(idle / 2)
		c.EP.SocketOptions().SetKeepAlive(false)
		clock.Advance(idle)
		if probe(t, c.GetPacketNonBlocking, c.Port, c.IRS) {
			t.Fatal("keepalive while disabled")
		}
		c.EP.SocketOptions().SetKeepAlive(true)
		got := deadline(t, clock, c.GetPacketNonBlocking, c.Port, c.IRS)
		t.Logf("KEEPALIVE_WIRE reenabled_deadline=%t", got)
	})

	t.Run("Accepted", func(t *testing.T) {
		c, clock := newFlow(t)
		c.AcceptWithOptionsNoDelay(-1, header.TCPSynOptions{MSS: e2e.DefaultIPv4MSS, WS: -1})
		configure(t, c.EP)
		if !deadline(t, clock, c.GetPacketNonBlocking, testcontext.StackPort, c.IRS) {
			t.Fatal("missing keepalive on accepted endpoint")
		}
		t.Log("KEEPALIVE_WIRE accepted_deadline=true")
	})

	t.Run("Restore", func(t *testing.T) {
		c, clock := newFlow(t)
		c.Stack().SetAllowLiveTCPMigration(true)
		c.Stack().SetRemoveConf(true)
		c.CreateConnected(testcontext.TestInitialSequenceNumber, 30000, -1)
		configure(t, c.EP)
		if !deadline(t, clock, c.GetPacketNonBlocking, c.Port, c.IRS) {
			t.Fatal("missing source keepalive")
		}
		t.Log("KEEPALIVE_WIRE after_connect_deadline=true")
		rearm(t, c.EP)
		var image bytes.Buffer
		if _, err := state.Save(context.Background(), &image, c.Stack()); err != nil {
			t.Fatal(err)
		}
		if image.Len() == 0 {
			t.Fatal("empty checkpoint")
		}
		s, link := loadKeepaliveProbeStack(t, image.Bytes(), true, e2e.DefaultMTU)
		s.Restore()
		eps := s.RegisteredEndpoints()
		if len(eps) != 1 {
			t.Fatalf("restored endpoints=%d", len(eps))
		}
		ep := eps[0].(tcpip.Endpoint)
		if tcp.EndpointState(ep.State()) != tcp.StateEstablished || !ep.SocketOptions().GetKeepAlive() {
			t.Fatal("expected restored established endpoint with SO_KEEPALIVE")
		}
		restoredClock := s.Clock().(*faketime.ManualClock)
		if restoredClock == clock || restoredClock.NowMonotonic() != clock.NowMonotonic() {
			t.Fatal("expected a separate restored clock at checkpoint time")
		}
		read := func() *buffer.View {
			pkt := link.Read()
			if pkt == nil {
				return nil
			}
			defer pkt.DecRef()
			return pkt.ToView()
		}
		if v := read(); v != nil {
			v.Release()
			t.Fatal("unexpected queued packet immediately after restore")
		}
		got := deadline(t, restoredClock, read, c.Port, c.IRS)
		t.Logf("KEEPALIVE_WIRE untouched_restore_deadline=%t", got)
		rearm(t, ep)
		if !deadline(t, restoredClock, read, c.Port, c.IRS) {
			t.Fatal("missing keepalive after restored endpoint's idle option was set")
		}
		t.Log("KEEPALIVE_WIRE rearmed_restore_deadline=true")
	})
}
