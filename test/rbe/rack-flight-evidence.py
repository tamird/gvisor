"""Run the matched packet, copy-layout and owner checks in one coordinator."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys


temp = Path(os.environ["RUNNER_TEMP"])
product = os.environ["PRODUCT"]
base = "bc87c0ee4bef70580104e832095bc89a93029440"
results = temp / "records"
tcp = "//pkg/tcpip/transport/tcp"
e2e = tcp + "/test/e2e:"
rack = e2e + "tcp_rack_test"
gsos = e2e + "tcp_test"
unit = tcp + ":tcp_test"
sack = e2e + "tcp_sack_test"
directory = Path("pkg/tcpip/transport/tcp")
peer = directory / "test/e2e/tcp_rack_test.go"
name = "TestRACKRepairAboveHighestSACK"
records = {}


def now():
    return datetime.now(timezone.utc).isoformat()


def git(*args):
    return subprocess.check_output(["git", *map(str,args)],text=True)


def store():
    (results / "flight-verdict.json").write_text(json.dumps(dict(source=product,parent=base,records=records),indent=2)+"\n")


def baz(label, targets, filters="", mode="test", config="x86_64", race=False, expected=0):
    d=results/label
    d.mkdir(exist_ok=True)
    bepf=d/"bep"
    flags=["docker","exec","tcp-compiler","bazel","--bazelrc="+str(temp/"tcp.bazelrc"),mode,
           "--config="+config,"--jobs=60","--build_event_json_file="+str(bepf)]
    if race: flags.append("--config=race")
    if mode=="test":
        flags += ["--strategy=TestRunner=remote","--test_sharding_strategy=disabled","--nocache_test_results",
                  "--test_output=all","--test_timeout=300","--test_env=GO_TEST_WRAP_TESTV=1","--test_arg=-test.v"]
        if filters: flags.append("--test_filter="+filters)
    if label == "nogo": flags.append("--keep_going")
    timeout = "35m" if label == "nogo" else "14m"
    start=now()
    with (d/"console").open("w") as log:
        code=subprocess.run(["timeout","-k","30s",timeout,*flags,*targets],stdout=log,stderr=subprocess.STDOUT).returncode
    text=(d/"console").read_text(errors="replace")
    cases=re.findall(r"^\s*--- (PASS|FAIL|SKIP): (\S+) \(",text,re.M)
    events=[]
    if bepf.exists():
        for line in bepf.read_text(errors="replace").splitlines():
            try: events.append(json.loads(line))
            except json.JSONDecodeError: pass
    verdicts={e["id"]["testSummary"]["label"]:e["testSummary"].get("overallStatus") for e in events if "testSummary" in e}
    completed={e["id"]["targetCompleted"]["label"]:e["completed"].get("success",False) for e in events
               if "targetCompleted" in e["id"] and "completed" in e}
    ids=[e["id"]["started"] for e in events if "started" in e["id"]]
    inv=[e["started"].get("uuid") for e in events if "started" in e]
    required=set(targets)
    witnessed=set(verdicts if mode=="test" else (t for t,v in completed.items() if v))
    caseok=mode=="build" or bool(cases)
    receipt=dict(start=start,end=now(),code=code,expected=expected,invocations=inv,targets=targets,missing=sorted(required-witnessed),
                 statuses=verdicts,count={k:sum(state==k for state,_ in cases) for k in ("PASS","FAIL","SKIP")},
                 skipped=[n for st,n in cases if st=="SKIP"],failed=[n for st,n in cases if st=="FAIL"],
                 selected=[c for c in cases if c[1].startswith((name,"TestPartialGSOProgress","TestACKForRefusedRetransmission","TestRefusedWriteBehindFlight","TestSACKBridgesRetransmittedQueueFragment","TestRACKRepeatedRepairHistory","TestPartialRepairPrefix"))],
                 observations=[x[:520] for x in text.splitlines() if any(k in x for k in ("COPY later_delivered","HISTORY ack_full","HISTORY ack_short","RACK_ACCEPT","FLIGHT_SPLIT","second repair flight exceeded cwnd"))],
                 errors=[x[:350] for x in text.splitlines() if x.startswith(("ERROR:","FAIL:")) and not any(z in x.lower() for z in ("header","key","token"))][-8:])
    receipt["complete"]=(code==expected and required<=witnessed and caseok)
    if expected: receipt["complete"] &= ("FAIL",name) in cases
    if label=="product":
        receipt["complete"] &= all(("PASS",x) in cases for x in (name,"TestPartialGSOProgress/ack","TestPartialGSOProgress/timeout","TestPartialGSOProgress/restore","TestACKForRefusedRetransmission","TestRefusedWriteBehindFlight"))
    if label=="layout":
        receipt["complete"] &= all(("PASS",x) in cases for x in ("TestRACKRepeatedRepairHistory","TestSACKBridgesRetransmittedQueueFragment","TestPartialRepairPrefix"))
    records[label]=receipt
    store()


def instrument():
    with peer.open("a") as f:f.write("\n"+(temp/"rack-history-peer.go.txt").read_text())
    p=directory/"rack_test.go"
    s=p.read_text().replace("s.writeList.PushBack(first)\n\ts.splitSeg(first, 300)",
        "first.originalInFlight = first.lastTx\n\ts.writeList.PushBack(first)\n\ts.splitSeg(first, 300)")
    marker="middle.lastTx = packetLayout{start: 2300, mss: 400}"
    assert s.count(marker)==1
    s=s.replace(marker,marker+"""
    middle.repairInFlight=true
    flight:=func()int {return s.rackPipe(first)+s.rackPipe(middle)+s.rackPipe(last)}
    if got:=flight(); got!=2 {t.Fatalf("split original plus middle retry: %d in flight, want 2",got)}
    s.ep.scoreboard.Insert(header.SACKBlock{Start:2300,End:2700})
    if got:=flight();got!=1 {t.Fatalf("SACKed middle retry: %d in flight, want only the original",got)}
    s.SndUna=2500
    if got:=flight();got!=1 {t.Fatalf("cumulative ACK through middle: %d in flight, want one original",got)}
    s.SndUna=0
    s.ep.scoreboard.Reset()
    t.Log("FLIGHT_SPLIT original=1000 middle_retry=400 before_ACK=2 after_SACK=1 after_partial_ACK=1")
""")
    p.write_text(s)
    p=directory/"snd.go"
    s=p.read_text(); marker="\tif err != nil && seg.payloadSize() != 0 {"
    assert s.count(marker)==1
    s=s.replace(marker,"""    if err!=nil && seg.payloadSize()!=0 {
        nextOriginal,nextRepair:=false,false
        if rest:=seg.Next();rest!=nil{nextOriginal,nextRepair=rest.originalInFlight.mss!=0,rest.repairInFlight}
        fmt.Printf("RACK_ACCEPT n=%d xmits=%d original=%t repair=%t nextOriginal=%t nextRepair=%t\\n",n,seg.xmitCount,seg.originalInFlight.mss!=0,seg.repairInFlight,nextOriginal,nextRepair)
    }
"""+marker)
    p.write_text(s)
    p=directory/"test/e2e/tcp_gso_write_test.go"
    with p.open("a") as f:f.write("""
func TestPartialRepairPrefix(t *testing.T) {
    g:=newGSOWriteTest(t)
    g.write(g.data)
    for i:=0;i<3;i++{g.ReceiveAndCheckPacket(g.data,i*g.mss,g.mss)}
    g.advanceRTO()
    g.ReceiveAndCheckPacket(g.data,0,g.mss)
    g.refuse.Store(true)
    g.allowOne.Store(true)
    ack:=func(n int) {
        g.probing.Store(true)
        g.SendAck(testcontext.TestInitialSequenceNumber+1,n*g.mss)
        select {case <-g.sender:case <-time.After(3*time.Second):t.Fatal("ACK not processed")}
    }
    ack(1)
    g.ReceiveAndCheckPacket(g.data,g.mss,g.mss)
    g.CheckNoPacketTimeout("unaccepted suffix appeared at peer",10*time.Millisecond)
    g.refuse.Store(false)
    ack(2)
    g.ReceiveAndCheckPacket(g.data,2*g.mss,g.mss)
    ack(3)
}
""")
    subprocess.run(["gofmt","-w",str(peer),str(directory/"rack_test.go"),str(directory/"snd.go"),str(p)],check=True)
    (results/"probe.patch").write_text(git("diff","--",directory))


git("checkout","--quiet","--detach",product)
fixture=peer.read_text().split("// TestRACKRepairAboveHighestSACK ",1)[1]
git("checkout","--quiet","--detach",base)
with peer.open("a") as f:f.write("\n// TestRACKRepairAboveHighestSACK "+fixture)
baz("parent",[rack],"^"+name+"$",expected=3)
git("restore","--source="+base,"--",peer)
git("checkout","--quiet","--detach",product)
baz("product",[unit,gsos,rack,sack])
baz("race",[unit,gsos,rack],"^(TestRACK.*|TestPartialGSOProgress|TestACKForRefusedRetransmission|TestRefusedWriteBehindFlight)$",race=True)
owners=[tcp+":tcp_nogo",e2e+"tcp_rack_test_nogo",e2e+"tcp_test_nogo"]
for arch in ("x86_64","aarch64"):
    baz("nogo-"+arch,owners,mode="build",config=arch)
instrument()
baz("layout",[unit,gsos,rack],"^(TestRACKRepeatedRepairHistory|TestSACKBridgesRetransmittedQueueFragment|TestPartialRepairPrefix|TestACKForRefusedRetransmission|TestPartialGSOProgress)$")
sys.exit(not all(r["complete"] for r in records.values()))
