"""Apply the two previously validated source selections to the same WAN sender."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

BASE = "b0d2148f41d2d35577b5fe759192f71947cc87fd"
DEADLINE = "9df20cfc54ca29cd875845a5e6e9fc9e6ac5c9f8"
SELECT = "c7976270813d5218a847539dd94f749c7cb4dfd5"
pkg = Path("pkg/tcpip/transport/tcp")


def show(commit, path):
    return subprocess.check_output(["git", "show", f"{commit}:{path}"], text=True)


def once(source, before, after):
    assert source.count(before) == 1, before
    return source.replace(before, after)


def digest(source):
    return hashlib.sha256(source.encode()).hexdigest()


def prepare(arm, output):
    assert arm in ("stock", "deadline", "select", "both")
    manifest = {"arm": arm, "base": BASE, "deadline_source": DEADLINE, "selection_source": SELECT, "files": {}}
    for name, receiver, indent, event in (("snd.go", "s", "\t\t\t", "rack-ack"), ("rack.go", "rc.snd", "\t\t", "rack-timer")):
        path = pkg / name
        source = show(BASE, path)
        entry = f"{receiver}.cc.HandleLossDetected()\n{indent}{receiver}.enterRecovery()\n{indent}fastRetransmit = "
        if arm in ("select", "both"):
            assert "retransmitHead = snd.writeList.Front().lost" in show(SELECT, pkg / "rack.go")
            source = once(source, entry + "true", entry + receiver + ".writeList.Front().lost")
        if name == "snd.go" and arm in ("deadline", "both"):
            upstream = show(DEADLINE, path)
            start = upstream.index("\tif seg.payloadSize() != 0 {\n\t\tswitch {\n\t\tcase err != nil:")
            replacement = upstream[start:upstream.index("\n\t}", start) + 3]
            start = source.index("\tif err != nil && seg.payloadSize() != 0 {\n\t\tif (s.FastRecovery.Active && seg.xmitCount")
            source = source[:start] + replacement + source[source.index("\n\t}", start) + 3:]
        product_hash = digest(source)
        if name == "snd.go":
            site = "\tep.snd.updateMaxPayloadSize(int(ep.snd.ep.route.MTU()), 0)"
            names = ["resend", "reorder", "probe", "cork"]
            source = once(source, site, "\n".join(
                f'\tep.snd.{n}Timer.m003Owner, ep.snd.{n}Timer.m003Name = ep.snd, "{n}"' for n in names
            ) + "\n" + site)
            marker = "\t// On a send error, keep the conservative timer"
            source = once(source, marker, (
                '\tif M003HeadEvent != nil && err == nil && seg.payloadSize() != 0 && seg.xmitCount > 1 && seg == s.writeList.Front() {\n'
                '\t\tM003HeadEvent(uint32(seg.sequenceNumber), "successful-head-send", int(seg.xmitCount), 0)\n'
                '\t}\n'
            ) + marker)
            source += "\nvar M003HeadEvent func(head uint32, kind string, count int, delay time.Duration)\n"
        observation = f'{receiver}.m003Observe("{event}", nil, 0)'
        if name == "snd.go":
            observation = ('if rcvdSeg.hasNewSACKInfo && len(rcvdSeg.parsedOptions.SACKBlocks) > 0 {\n'
                           + indent + '\ts.m003Observe("rack-ack-new-sack", rcvdSeg.parsedOptions.SACKBlocks, rcvdSeg.rcvdTime.Sub(tcpip.MonotonicTime{}).Nanoseconds())\n'
                           + indent + '} else {\n' + indent + '\t' + observation + '\n' + indent + '}')
        source = once(source, entry, observation + "\n" + indent + entry)
        if name == "snd.go":
            marker = "\ts.ep.stack.Stats().TCP.Timeouts.Increment()"
            source = once(source, marker, '\ts.m003Observe("rto", nil, 0)\n' + marker)
            marker = "\t// On a send error, keep the conservative timer"
            source = once(source, marker, '\tif err == nil && seg.payloadSize() != 0 && s.FastRecovery.Active && s.ep.SACKPermitted && seg.xmitCount > 1 && seg == s.writeList.Front() {\n\t\ts.m003Observe("head-repair", nil, 0)\n\t}\n' + marker)
            source += '''
// M003RecoveryObservation passively records the sender decision and actual incoming SACKs.
var M003RecoveryObservation func(kind string, head uint32, blocks []header.SACKBlock, stamp int64, lost bool, rto time.Duration)
var M003RACKLoss func(head, start, end, latestEnd, count uint32, stamp int64, alreadyLost bool, headAge, holeAge, latestAge, rtt, reo, precision time.Duration)

func (s *sender) m003Observe(kind string, blocks []header.SACKBlock, stamp int64) {
	if M003RecoveryObservation != nil && s.writeList.Front() != nil {
		M003RecoveryObservation(kind, uint32(s.SndUna), blocks, stamp, s.writeList.Front().lost, s.RTO)
	}
}
'''
        else:
            marker = "\t\t\t\tseg.lost = true"
            observe = """                if M003RACKLoss != nil {
                    M003RACKLoss(uint32(rc.snd.SndUna), uint32(seg.sequenceNumber), uint32(endSeq), uint32(rc.EndSequence), seg.xmitCount, rcvTime.Sub(tcpip.MonotonicTime{}).Nanoseconds(), seg.lost, rcvTime.Sub(rc.snd.writeList.Front().xmitTime), rcvTime.Sub(seg.xmitTime), rcvTime.Sub(rc.XmitTime), rc.RTT, rc.ReoWnd, clockResolution)
                }
"""
            source = once(source, marker, observe + marker)
        path.write_text(source)
        manifest["files"][name] = {"policy": product_hash, "observed": digest(source)}
    path = pkg / "timer.go"
    timer = show(BASE, path)
    product_hash = digest(timer)
    timer = once(timer, "type timer struct {", "type timer struct {\n\tm003Owner *sender\n\tm003Name string")
    for before, code in (
        ("\tt.state = timerStateEnabled", '\tt.m003Event("arm", t.target.Sub(t.clock.NowMonotonic()))'),
        ("\t// The timer has actually expired", '\tt.m003Event("expire", 0)'),
        ("func (t *timer) disable() {", '\tif t.enabled() { t.m003Event("disable", 0) }'),
    ):
        block = before+"\n"+code if before.endswith("{") or "timerStateEnabled" in before else code+"\n"+before
        timer = once(timer, before, block)
    timer += '''
func (t *timer) m003Event(op string, left time.Duration) {
	if t.m003Owner != nil && M003HeadEvent != nil {
		M003HeadEvent(uint32(t.m003Owner.SndUna), t.m003Name+"-"+op, 0, left)
	}
}
'''
    path.write_text(timer)
    manifest["files"][path.name] = {"policy": product_hash, "observed": digest(timer)}
    Path(output).write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    prepare(*sys.argv[1:])
