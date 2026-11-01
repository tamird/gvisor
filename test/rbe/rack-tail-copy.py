"""Compare original-loss and repair contributions for one half-delivered flight."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone


def swap(path, before, after):
    text = path.read_text()
    if text.count(before) != 1:
        raise ValueError((path, before))
    path.write_text(text.replace(before, after))


def prepare(arm):
    sender = Path("pkg/tcpip/transport/tcp/snd.go")
    rack = sender.with_name("rack.go")
    segment = sender.with_name("segment.go")
    peer = Path("pkg/tcpip/transport/tcp/test/e2e/tcp_rack_test.go")
    with peer.open("a") as out:
        out.write("\n" + (temp / "rack-tail-peer.go.txt").read_text())
    original = "!s1.lost && !s.ep.scoreboard.IsRangeLost(sb)"
    marker = "s1.sequenceNumber.LessThanEq(s.FastRecovery.HighRxt)"
    new_marker = marker
    if arm != "measured":
        # Paste the successful-send and copy-selection expressions from a1da0f98e.
        swap(sender, "err := s.sendSegmentFromPacketBuffer(seg.pkt, seg.flags, seg.sequenceNumber)\n",
             """err := s.sendSegmentFromPacketBuffer(seg.pkt, seg.flags, seg.sequenceNumber)
    if err == nil && s.FastRecovery.Active && s.ep.tcpRecovery&tcpip.TCPRACKLossDetection != 0 && seg.xmitCount > 1 && seg.payloadSize() != 0 {
        last := seg.sequenceNumber.Add(seqnum.Size(seg.payloadSize())) - 1
        if s.FastRecovery.HighRxt.LessThan(last) { s.FastRecovery.HighRxt = last }
    }
""")
        new_marker = "((s.ep.tcpRecovery&tcpip.TCPRACKLossDetection == 0 && " + marker + ") || (s.ep.tcpRecovery&tcpip.TCPRACKLossDetection != 0 && startSeq.LessThanEq(s.FastRecovery.HighRxt) && s1.xmitCount > 1 && !s1.lost))"
        swap(sender, "if " + marker + " {", "if " + new_marker + " {")
    if arm == "remembered":
        swap(segment, "\tlost bool", "\tlost bool\n\toriginalLost bool")
        swap(segment, "t.lost = s.lost", "t.lost = s.lost\n\tt.originalLost = s.originalLost")
        swap(rack, "seg.lost = true", "seg.lost = true\n\t\t\t\tseg.originalLost = true")
        swap(sender, "if " + original + " {", "if !s1.originalLost && " + original + " {")
        original = "!s1.originalLost && " + original
    swap(sender, "if " + original + " {", """if smss == 10 && s.SndCwnd == 5 && startSeq.Sub(s.SndUna) < 130 {
       fmt.Printf("COPYCOUNT una=%d offset=%d lost=%t sacklost=%t count=%d original=%t repair=%t\\n",uint32(s.SndUna),startSeq.Sub(s.SndUna),s1.lost,s.ep.scoreboard.IsRangeLost(sb),s1.xmitCount,""" + original + "," + new_marker + """)
    }
    if """ + original + " {")
    # seqnum.Value has a wrap-aware Size method; raw unsigned distance is
    # sufficient within the thirteen-packet diagnostic window.
    text=sender.read_text().replace("startSeq.Sub(s.SndUna)", "uint32(startSeq-s.SndUna)")
    sender.write_text(text)
    swap(rack, "seg.lost = true", 'fmt.Printf("COPYLOSS seq=%d xmits=%d\\n",uint32(seg.sequenceNumber),seg.xmitCount)\n                seg.lost = true')
    if '"fmt"' not in rack.read_text():
        swap(rack, 'import (', 'import (\n    "fmt"')
    subprocess.run(["gofmt", "-w", str(sender), str(rack), str(segment), str(peer)], check=True)


def observe(arm):
    d = temp / "records" / arm
    log = (d / "log").read_text(errors="replace")
    rows = [line.strip() for line in log.splitlines() if "COPY ack=" in line]
    counts = [line.strip() for line in log.splitlines() if line.startswith("COPYCOUNT ")]
    ack = {int(m[1]): (int(m[2]), [int(n) for n in m[3].split()])
           for line in rows if (m := re.search(r"COPY ack=(\d+) absolute=(\d+).* tx=\[([^\]]*)\]", line))}
    issues = [line[:300] for line in log.splitlines() if line.lstrip().startswith(("ERROR:", "FAIL:", "--- FAIL:", "rack", "tcp_rack_test.go:")) and not any(x in line.lower() for x in ("header", "token", "api-key"))][-14:]
    selected = [line for line in counts if 2 in ack and f"una={ack[2][0]} " in line]
    good = (d/"exit").read_text().strip()=="0" and sorted(ack)==[1,2] and ack[1][1]==[2,3,7,8,9] and "--- PASS: TestRACKOriginalAndRepairFlight " in log
    return dict(complete=good,acks=rows,second_count=selected,marks=[ln for ln in log.splitlines() if ln.startswith("COPYLOSS ")][-64:],diagnostics=issues,invocations=list(dict.fromkeys(re.findall(r"https://app.buildbuddy.io/invocation/([a-f0-9-]{36})",log))))


temp = Path(os.environ["RUNNER_TEMP"])
if sys.argv[1] == "prepare":
    prepare(sys.argv[2])
else:
    all_arms = {arm:observe(arm) for arm in ("measured","previous","remembered")}
    path=temp/"records"/"verdict.json"
    path.write_text(json.dumps(dict(source=os.environ["PRODUCT"],arms=all_arms,recorded=datetime.now(timezone.utc).isoformat()),indent=2)+"\n")
    sys.exit(not all(arm["complete"] for arm in all_arms.values()))
