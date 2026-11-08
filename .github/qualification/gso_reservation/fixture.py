"""Extend the existing public socket fixture identically in both source builds."""
from pathlib import Path


def once(text, original, replacement):
    if text.count(original) != 1:
        raise ValueError(original)
    return text.replace(original, replacement)


def install(directory):
    tcp = Path("pkg/tcpip/transport/tcp")
    build = tcp / "BUILD"
    before, target = build.read_text().rsplit("go_test(", 1)
    target = once(target, '"endpoint_test.go",', '"endpoint_test.go",\n        "gso_reservation_test.go",')
    target = once(target, '"//pkg/tcpip/header",', '"//pkg/tcpip/checksum",\n        "//pkg/tcpip/header",')
    target = once(target, '"//pkg/tcpip/network/ipv4",', '"//pkg/tcpip/network/ipv4",\n        "//pkg/tcpip/network/ipv6",')
    build.write_text(before + "go_test(" + target)
    (tcp / "gso_reservation_test.go").write_bytes((directory / "probe.go.inc").read_bytes())

    path = tcp / "test/e2e/tcp_test.go"
    text = path.read_text()
    text = once(text, '"bytes"\n', '"bytes"\n\tstdcontext "context"\n')
    text = once(text, '"gvisor.dev/gvisor/pkg/tcpip/link/loopback"', '"gvisor.dev/gvisor/pkg/tcpip/link/channel"\n\t"gvisor.dev/gvisor/pkg/tcpip/link/loopback"')
    start = text.index("func TestSetExperimentOptionIPv6(")
    stop = text.index("\nfunc ", start + 2)
    test = text[start:stop]
    test = once(test, "func TestSetExperimentOptionIPv6(t *testing.T) {", '''func TestSetExperimentOptionIPv6(t *testing.T) {
    for _, opt := range []uint16{0, 99} {
        t.Run(fmt.Sprintf("option_%d", opt), func(t *testing.T) { testSocketGSO(t, opt) })
    }
}

func testSocketGSO(t *testing.T, opt uint16) {''')
    test = once(test, "e2e.DefaultMTU", "1500")
    test = once(test, "defer c.Cleanup()", '''defer c.Cleanup()
    dev := c.Stack().GetLinkEndpointByName("nic1")
    if wrapped, ok := dev.(*sniffer.Endpoint); ok {
        dev = wrapped.Child()
    }
    link := dev.(*channel.Endpoint)
    link.SupportedGSOKind = stack.GVisorGSOSupported''')
    test = once(test, "TCPOpts: nil,", "TCPOpts: []byte{2, 4, 0x05, 0xa0}, // Peer MSS 1440.")
    test = test[:test.index("\tvar expval uint16")] + '''    c.EP.SocketOptions().SetExperimentOptionValue(opt)
    c.EP.SocketOptions().SetDelayOption(false)
    const mss = 1440
    payload := make([]byte, 7*mss+73)
    for i := range payload { payload[i] = byte(i*29+i/251) }
    if n, err := c.EP.Write(bytes.NewReader(payload), tcpip.WriteOptions{}); err != nil || n != int64(len(payload)) {
        t.Fatalf("write = %d, %v", n, err)
    }
    deadline, cancel := stdcontext.WithTimeout(t.Context(), 8*time.Second)
    defer cancel()
    for off := 0; off < len(payload); off += mss {
        p := link.ReadContext(deadline)
        if p == nil { t.Fatal("missing data packet") }
        v := p.ToView()
        defer v.Release()
        defer p.DecRef()
        size := min(mss, len(payload)-off)
        checks := []checker.NetworkChecker{checker.TCP(checker.SrcPort(c.Port), checker.DstPort(context.TestPort), checker.TCPSeqNum(uint32(c.IRS)+1+uint32(off)), checker.TCPAckNum(uint32(iss)+1), checker.TCPFlagsMatch(header.TCPFlagAck,header.TCPFlagAck), checker.Payload(payload[off:off+size]))}
        if opt == 0 { checks = append(checks, checker.IPv6ExtHdr()) } else { checks = append(checks, checker.IPv6ExtHdr(checker.IPv6ExperimentHeader(opt))) }
        checker.IPv6WithExtHdr(t, v, checks...)
        if p.GSOOptions.Type != stack.GSOGvisor || p.GSOOptions.MSS != mss { t.Fatalf("unexpected GSO: %+v", p.GSOOptions) }
        t.Logf("SOCKET_PACKET opt=%d offset=%d data=%d reserve=%d available=%d wire=%d", opt, off, size, p.ReservedHeaderBytes(), p.AvailableHeaderBytes(), v.Size())
    }
    c.SendV6Packet(nil, &context.Headers{SrcPort:context.TestPort, DstPort:c.Port, Flags:header.TCPFlagAck, SeqNum:iss.Add(1), AckNum:c.IRS.Add(seqnum.Size(1+len(payload))), RcvWnd:rcvWnd})
}
'''
    path.write_text(text[:start] + test + text[stop:])
