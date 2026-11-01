"""Observe a locally refused first packet and existing vs new SACK evidence."""

from pathlib import Path
import os


common = (Path(os.environ["RUNNER_TEMP"]) / "rack-flight-evidence.py").read_text()
common = common[:common.index("\ngit(\"checkout\",\"--quiet\",\"--detach\",product)")]
common = common.replace('("COPY later_delivered",', '("FIRST ", "FIRST_TX ", "FLIGHT_SIZE ", "COPY later_delivered",')
exec(compile(common, "rack-flight-evidence.py", "exec"))


def definition(s, name):
    return "type " + name + " struct {" + s.split("type segment struct {", 1)[1].split("\n}", 1)[0] + "\n}\n"


def compact(s):
    s=s.replace("\n\tqFlags queueFlags", "").replace("\n\thasNewSACKInfo bool","").replace("\n\trepairInFlight   bool","")
    assert s.count("\n\tacked bool")==1
    return s.replace("\n\tacked bool","\n\tacked bool\n\thasNewSACKInfo bool\n\trepairInFlight bool\n\tqFlags queueFlags")


def fixture():
    p=peer
    s=p.read_text().replace('"os"','"os"\n "sync/atomic"').replace('"gvisor.dev/gvisor/pkg/tcpip/seqnum"','"gvisor.dev/gvisor/pkg/tcpip/seqnum"\n "gvisor.dev/gvisor/pkg/tcpip/stack"')
    p.write_text(s+(temp/"rack-attempt-peer.go.txt").read_text())
    p=directory/"test/e2e/BUILD"
    s=p.read_text();i=s.index('    name = "tcp_rack_test"')
    p.write_text(s[:i]+s[i:].replace('        "//pkg/tcpip/seqnum",','        "//pkg/tcpip/seqnum",\n        "//pkg/tcpip/stack",',1))
    p=directory/"segment.go";s=p.read_text()
    orig=git("show",base+":"+str(p))
    p.write_text(s+"\n"+definition(orig,"flightParentSegment")+definition(compact(s),"flightPackedSegment"))
    p=directory/"segment_unsafe.go";s=p.read_text()
    p.write_text(s+"\nconst flightParentSize=unsafe.Sizeof(flightParentSegment{})\nconst flightPackedSize=unsafe.Sizeof(flightPackedSegment{})\n")
    p=directory/"segment_test.go";s=p.read_text().replace('"testing"','"testing"\n"runtime"')
    p.write_text(s+"""
func TestFlightAccountSize(t *testing.T) {
    var ms runtime.MemStats
    runtime.ReadMemStats(&ms)
    class:=func(n int)int{ for _,c:=range ms.BySize{ if int(c.Size)>=n{return int(c.Size)} };return -1 }
    t.Logf("FLIGHT_SIZE arch=%s parent=%d current=%d packed=%d parentClass=%d currentClass=%d packedClass=%d",runtime.GOARCH,flightParentSize,segSize,flightPackedSize,class(int(flightParentSize)),class(segSize),class(int(flightPackedSize)))
}
""")


def instrument_send(variant):
    p=directory/"snd.go"
    s=git("show",product+":"+str(p))
    start=s.index("func (s *sender) sendSegment(seg *segment) sendResult {")
    before,s=s[:start],s[start:]
    s=s.replace("now := s.ep.stack.Clock().NowMonotonic()", """beforeTime:=seg.xmitTime
    beforeCount:=seg.xmitCount
    firstWire:=seg.lastTx.mss==0
    now := s.ep.stack.Clock().NowMonotonic()""",1)
    if variant!="attempt":
        s=s.replace("if seg.xmitCount == 1 {", "if firstWire {",1)
        if variant=="epoch":
            s=s.replace("if firstWire {", "if firstWire && (beforeCount==0 || s.ep.scoreboard.Empty() || !s.ep.scoreboard.IsRangeLost(seg.sackBlock())) {",1)
            s=s.replace("seg.xmitTime = now", "if err==nil || n!=0 || firstWire {seg.xmitTime = now}",1)
    marker="\tif err != nil && seg.payloadSize() != 0 {"
    assert s.count(marker)==1
    s=s.replace(marker,"""    if seg.payloadSize()!=0 && (err!=nil || beforeCount!=0) {
        fmt.Printf("FIRST_TX variant=VAR rel=%d len=%d priorAttempts=%d hadWire=%t err=%t n=%d xmitDelta=%v scoreLost=%t explicitLost=%t original=%t repair=%t pipe=%d cwnd=%d active=%t\\n",int32(seg.sequenceNumber-s.SndUna),seg.payloadSize(),beforeCount,!firstWire,err!=nil,n,seg.xmitTime.Sub(beforeTime),s.ep.scoreboard.IsRangeLost(seg.sackBlock()),seg.lost,seg.originalInFlight.mss!=0,seg.repairInFlight,s.rackPipe(seg),s.SndCwnd,s.FastRecovery.Active)
    }
""".replace("VAR",variant)+marker)
    p.write_text(before+s)


git("checkout","--quiet","--detach",product)
fixture()
for variant in ("attempt","wire","epoch"):
    instrument_send(variant)
    baz(variant,[rack,gsos],"^(TestRACKFirstAccepted|TestACKForRefusedRetransmission)$")
    rec=records[variant]
    rec["complete"] &= all(any(name in v for v in rec["observations"]) for name in ("FIRST before-retry","FIRST followup"))
    store()
baz("size-amd64",[unit],"^TestFlightAccountSize$")
size="\n".join(records["size-amd64"]["observations"])
m=re.search(r"FLIGHT_SIZE arch=amd64 parent=(\d+) current=(\d+) packed=(\d+) parentClass=(\d+) currentClass=(\d+) packedClass=(\d+)",size)
if m:
    old,actual,packed,*_=m.groups()
    p=directory/"segment_unsafe.go"
    with p.open("a") as f:
        for name,value in (("flightParentSize",old),("segSize",actual),("flightPackedSize",packed)):
            f.write("var _ ["+value+"]byte = ["+name+"]byte{}\n")
    baz("size-arm64",[tcp+":tcp"],mode="build",config="aarch64")
    (results/"size.json").write_text(json.dumps({"linuxAmd64Observed":m.group(0),"arm64CompileAssertions":records["size-arm64"]},indent=2)+"\n")
store()
sys.exit(not m or not all(r["complete"] for r in records.values()))
