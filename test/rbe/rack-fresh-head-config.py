"""Set classic SACK as the default for this disposable gVisor test stack."""
import sys
from pathlib import Path

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
