"""Set classic SACK as the default for this disposable gVisor test stack."""
import sys
from pathlib import Path

arm = sys.argv[1]
if arm in ("late_fixed", "late_mutant"):
    p = Path("test/packetimpact/tests/tcp_retransmits_test.go")
    source = p.read_text()
    start = source.index("func TestLimitedTransmit(")
    end = source.index("\n}\n", start)
    fixture = source[start:end].replace("// Fill the congestion window", "start := time.Now()\n\t// Fill the congestion window")
    fixture = fixture[:fixture.index("\tdut.Send(t, fd, make([]byte, 2*info.SndMss), 0)")] + """
    for i := 0; i < 3; i++ {
        conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), AckNum: &base})
        time.Sleep(12 * time.Millisecond)
    }
    dut.Send(t, fd, payload.Bytes, 0)
    conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), AckNum: &base})
    seq := base + info.SndCwnd*info.SndMss
    if _, err := conn.ExpectData(t, &testbench.TCP{SeqNum: &seq}, payload, 25*time.Millisecond); err == nil {
        t.Fatalf("fourth duplicate ACK sent packet %d queued after the third", info.SndCwnd)
    }
    elapsed := time.Since(start)
    after := dut.GetSockOptTCPInfo(t, fd)
    if elapsed >= time.Duration(info.RTO)*time.Microsecond || after.CaState > 1 || after.SndCwnd != info.SndCwnd {
        t.Fatalf("unexpected late state: elapsed=%v rto=%d state=%d cwnd=%d", elapsed, info.RTO, after.CaState, after.SndCwnd)
    }
    conn.Send(t, testbench.TCP{Flags: testbench.TCPFlags(header.TCPFlagAck), AckNum: &seq})
    expect(info.SndCwnd)
    t.Logf("LATE withheld until cumulative ACK: cwnd=%d elapsed_us=%d rto_us=%d", info.SndCwnd, elapsed.Microseconds(), info.RTO)
"""
    p.write_text(source[:start] + fixture + source[end:])
    if arm == "late_mutant":
        sender = Path("pkg/tcpip/transport/tcp/snd.go")
        source = sender.read_text()
        old = "dupAck && !s.inRecovery() && s.limitedTransmitACKs < 2"
        assert source.count(old) == 1
        sender.write_text(source.replace(old, "dupAck && !s.inRecovery() && s.DupAckCount < 2"))
    sys.exit()

protocol = Path("pkg/tcpip/transport/tcp/protocol.go")
s = protocol.read_text()
old = "recovery:                   tcpip.TCPRACKLossDetection,"
assert s.count(old) == 1, "unknown recovery default"
if sys.argv[1] != "rack":
    protocol.write_text(s.replace(old, "recovery:                   0,"))

p = Path("test/packetimpact/tests/tcp_rack_test.go")
s = p.read_text()
if '"fmt"' not in s:
    assert s.count('"flag"') == 1
    s = s.replace('"flag"', '"flag"\n\t"fmt"')
p.write_text(s)
