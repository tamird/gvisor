"""Observe a locally refused first packet and existing vs new SACK evidence."""

from pathlib import Path
import os


common = (Path(os.environ["RUNNER_TEMP"]) / "rack-flight-evidence.py").read_text()
common = common[:common.index("\ngit(\"checkout\",\"--quiet\",\"--detach\",product)")]
common = common.replace('("COPY later_delivered",', '("FULLWIN ", "FIRST ", "FIRST_TX ", "FLIGHT_SIZE ", "COPY later_delivered",')
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


def observe_window():
    p=peer;s=p.read_text();pos=s.index("func TestRACKWithWindowFull(");x=s.index("\n}",pos)+2
    t=s[pos:x].replace("c := context.New(t, uint32(mtu))", """windows:=make(chan tcp.TCPSenderState,16)
    c:=context.NewWithOpts(t,context.Options{MTU:mtu,EnableV4:true,Probe:func(st *tcp.TCPEndpointState){windows<-st.Sender}})""")
    t=t.replace("// Send ACK with zero window size.", """for {
        select{case st:=<-windows:if st.SndUna==c.IRS.Add(1+3*maxPayload){goto beforeZero};case <-time.After(5*time.Second):t.Fatal("FULLWIN ACK 3 never processed")}
    }
beforeZero:
    dump:=func(phase string,p *buffer.View){
        h:=header.TCP(header.IPv4(p.AsSlice()).Payload());t.Logf("FULLWIN phase=%s seq=%d payload=%d flags=%d",phase,int32(h.SequenceNumber()-uint32(c.IRS+1)),len(h.Payload()),h.Flags());p.Release()
    }
    for p:=c.GetPacketNonBlocking();p!=nil;p=c.GetPacketNonBlocking(){dump("before",p)}
    // Send ACK with zero window size.""")
    t=t.replace('c.CheckNoPacket("unexpected packet received after userTimeout has expired")', """for {
        select{case st:=<-windows:if st.SndUna==c.IRS.Add(1+4*maxPayload) && st.SndWnd==0{goto zero};case <-time.After(5*time.Second):t.Fatal("FULLWIN zero window not processed")}
    }
zero:
    end:=time.Now().Add(time.Second)
    for time.Now().Before(end) {p:=c.GetPacketWithTimeout(time.Until(end));if p==nil{break};dump("after",p)}
    t.Log("FULLWIN checked zero window for one second")""")
    p.write_text(s[:pos]+t+s[x:])


for src, label in ((base,"base"),(product,"product")):
    git("checkout","--quiet","--detach",src)
    observe_window()
    baz(label+"-zero",[rack],"^TestRACKWithWindowFull$")
    git("restore","--source="+src,"--",peer)
git("checkout","--quiet","--detach",product)
fixture()
for variant in ("attempt","wire","epoch"):
    instrument_send(variant)
    baz(variant,[rack],"^TestRACKFirstAccepted$")
    rec=records[variant]
    rec["complete"] &= all(any(name in v for v in rec["observations"]) for name in ("FIRST before-retry","FIRST followup"))
    store()
sys.exit(not all(r["complete"] for r in records.values()))
