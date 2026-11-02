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
        observation = f'{receiver}.m003Observe("{event}", 0)'
        if name == "snd.go":
            observation = ('if rcvdSeg.hasNewSACKInfo && len(rcvdSeg.parsedOptions.SACKBlocks) > 0 {\n'
                           + indent + '\ts.m003Observe("rack-ack-new-sack", uint32(rcvdSeg.parsedOptions.SACKBlocks[0].End))\n'
                           + indent + '} else {\n' + indent + '\t' + observation + '\n' + indent + '}')
        source = once(source, entry, observation + "\n" + indent + entry)
        if name == "snd.go":
            marker = "\ts.ep.stack.Stats().TCP.Timeouts.Increment()"
            source = once(source, marker, '\ts.m003Observe("rto", 0)\n' + marker)
            marker = "\t// On a send error, keep the conservative timer"
            source = once(source, marker, '\tif err == nil && seg.payloadSize() != 0 && s.FastRecovery.Active && s.ep.SACKPermitted && seg.xmitCount > 1 && seg == s.writeList.Front() {\n\t\ts.m003Observe("head-repair", 0)\n\t}\n' + marker)
            source += '''
// M003RecoveryObservation records only the three recovery events used by the fork experiment.
var M003RecoveryObservation func(kind string, head, sackRight uint32, lost bool, rto time.Duration)

func (s *sender) m003Observe(kind string, sackRight uint32) {
	if M003RecoveryObservation != nil && s.writeList.Front() != nil {
		M003RecoveryObservation(kind, uint32(s.SndUna), sackRight, s.writeList.Front().lost, s.RTO)
	}
}
'''
        path.write_text(source)
        manifest["files"][name] = {"policy": product_hash, "observed": digest(source)}
    Path(output).write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    prepare(*sys.argv[1:])
