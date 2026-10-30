"""Report the two competing senders over the same portion of a transfer."""
import csv
import re
import json
import os
from pathlib import Path
from statistics import median
from datetime import datetime, timezone

def read_json(p):
    return json.loads(p.read_text())

def read_table(p):
    with p.open() as f:
        return list(csv.DictReader(f, delimiter="\t"))

def diagnostics(d):
    # The benchmark has no credentials; still retain only bounded, named
    # process output, not the environment, Bazel argv or arbitrary logs.
    import re
    result={}
    for file in ("exit.txt", "stdout", "stderr", "results/topology-cleanup-exit.txt", "results/ping-preflight.txt", "results/primary/flow-exit.txt", "results/secondary/flow-exit.txt", "results/primary/iperf-exit.txt", "results/secondary/iperf-exit.txt", "results/primary/receiver-stderr.txt", "results/secondary/receiver-stderr.txt", "results/primary/stderr.txt", "results/secondary/stderr.txt"):
        p=d/file
        if not p.exists(): continue
        lines=p.read_text(errors="replace").splitlines()
        keep=[l[-350:] for l in lines if l and not re.search(r"token|authorization|api[_-]?key|bearer|password|remote[_-]?header|WANSAMPLE",l,re.I)]
        result[file]=keep[-12:]
    for side in ("primary","secondary"):
        p=d/"results"/side/"receiver.json"
        if p.exists():
            try:
                j=read_json(p)
                result[f"{side}/receiver_summary"]={"error":j.get("error"),"received":j.get("end",{}).get("sum_received")}
            except ValueError: pass
    return result

def trial(root, mode, case, variant, round_number):
    d = root / mode / case
    cc, primary, secondary = case.split("-")
    assert (code := (d/"exit.txt").read_text().strip()) == "0", f"benchmark exit={code}"
    results = d/"results"
    assert (results/"topology-cleanup-exit.txt").read_text().strip() == "0"
    qdisc = (results/"qdisc-after.txt").read_text()
    before = (results/"qdisc-before.txt").read_text()
    for snapshot in (before, qdisc):
        lines = [l for l in snapshot.splitlines() if l.startswith("qdisc netem ")]
        assert len(lines) == 2 and all(f"limit {variant} " in l and re.search(r"delay +50(?:[.]0+)?ms\b", l) and re.search(r"rate +20(?:[.]0+)?Mbit\b", l, re.I) for l in lines), lines
    queue = {}
    for dev in ("wan.0", "wan.1"):
        match = re.search(r"(?m)^"+re.escape(dev)+r"\n[^\n]*\n\s*Sent (\d+) bytes (\d+) pkt \(dropped (\d+)", qdisc)
        assert match, qdisc
        queue[dev] = dict(zip(("bytes", "packets", "dropped"), map(int, match.groups())))
    assert "client2.0" in (results/"interface-features.txt").read_text()
    assert (results/"ping-exit.txt").read_text().strip() in ("0","1")
    ping_phases = read_table(results/"ping-phases.tsv")
    assert [p["phase"] for p in ping_phases] == ["probe-launch","flows-begin","flows-end","probe-stop"]
    sides = []
    for side, stack in [("primary",primary), ("secondary",secondary)]:
        p = results/side
        for key in ("cleanup","iperf","receiver","flow"):
            assert (p/f"{key}-exit.txt").read_text().strip() == "0", key
        assert f"congestion_control={cc}" in (p/"flow-settings.txt").read_text()
        assert f"client={stack} " in (p/"flow-settings.txt").read_text()
        assert f"-observation={mode}" in (p/"flow-settings.txt").read_text()
        j = read_json(p/"receiver.json")
        assert "error" not in j and j["start"]["test_start"]["num_streams"] == 1
        end = j["end"]["sum_received"]
        assert not end["sender"] and end["bytes"] == j["end"]["streams"][0]["receiver"]["bytes"]
        dur = end["seconds"]; assert 115 <= dur <= 135, dur
        intervals = [i["sum"] for i in j["intervals"]]
        assert intervals and all(not i["omitted"] and not i["sender"] and i["end"] >= i["start"] and i["bytes"] >= 0 for i in intervals)
        residual = end["bytes"]-sum(i["bytes"] for i in intervals)
        assert abs(residual) < 4e6, residual
        t = {r["phase"]:float(r["unix_seconds"]) for r in read_table(p/"flow-phases.tsv")}
        lo,hi = t["client-operation-begin"]-.01,t["client-operation-end"]+.01-dur
        assert 0 <= hi-lo < 4,(lo,hi)
        sides.append((p,stack,lo,hi,dur,intervals,residual))
    A=max(s[3] for s in sides)+1
    B=min(s[2]+s[4] for s in sides)-1
    assert B-A>100
    rates={}
    sockets={}
    for p,stack,lo,hi,dur,intervals,residual in sides:
        lower=sum(i["bytes"] for i in intervals if lo+i["start"]>=A and hi+i["end"]<=B)
        upper=sum(i["bytes"] for i in intervals if hi+i["end"]>A and lo+i["start"]<B)
        rates[p.name]=[max(0,lower+min(residual,0))*8/(B-A)/1e6,(upper+max(residual,0))*8/(B-A)/1e6]
        rows=[json.loads(l.split("WANSAMPLE ",1)[1]) for l in (p/"stderr.txt").read_text().splitlines() if "WANSAMPLE " in l]
        assert mode == "plain" and not rows
    a,b=rates["primary"],rates["secondary"]
    return dict(overlap_seconds=B-A,mbps_bounds=rates,primary_share_bounds=[a[0]/(a[0]+b[1]),a[1]/(a[1]+b[0])],queues=queue)

root=Path(os.environ["RUNNER_TEMP"])/"qualification/current-shared"
public=Path(os.environ["RUNNER_TEMP"])/"tcp-public"
public.mkdir(exist_ok=True)
cases=[f"{cc}-{primary}-{secondary}" for cc in ("reno","cubic") for primary in ("netstack","linux") for secondary in ("netstack","linux")]
expected=[(rnd,size,"plain",case) for rnd in range(2) for size in (100,256) for case in cases if rnd == 0 or case.split("-")[1]!=case.split("-")[2]]
verdict={"harness":os.environ["GITHUB_SHA"],"expected":len(expected),"passed":{},"failures":{},"missing":[],"skips":[],"reversals":[]}
for rnd,v,mode,case in expected:
    p=root/str(rnd)/str(v)
    key=f"{rnd}/{v}/{mode}/{case}"
    if not (p/mode/case/"exit.txt").exists():
        verdict["missing"].append(key); continue
    try:
        verdict["passed"][key]=trial(p,mode,case,v,rnd)
    except (AssertionError,KeyError,ValueError,IndexError,TypeError,OSError) as exc:
        verdict["failures"][key]={"reason":f"{type(exc).__name__}: {exc}","diagnostics":diagnostics(p/mode/case)}
verdict["sources"]=(root/"binaries.txt").read_text().splitlines() if (root/"binaries.txt").exists() else []
verdict["preflight"]={v:diagnostics(root/"preflight"/v) for v in ("netstack-netstack","linux-linux")}
verdict["execution_finished"]=(root/"trials-finished.txt").read_text().strip() if (root/"trials-finished.txt").exists() else None
verdict["driver"]=read_json(root/"completion.json") if (root/"completion.json").exists() else None
# Recover analysis without rerunning the traffic if a new iproute2 output
# spelling or an interval assertion fails. Only synthetic benchmark files.
import tarfile
with tarfile.open(public/"receipts.tar.gz", "w:gz") as archive:
    for rnd,size,mode,case in expected:
        directory=root/str(rnd)/str(size)/mode/case
        files=["exit.txt","results/topology-cleanup-exit.txt","results/qdisc-before.txt","results/qdisc-after.txt","results/ping-exit.txt","results/ping-phases.tsv","results/interface-features.txt"]
        for side in ("primary","secondary"):
            files += [f"results/{side}/{f}" for f in ("cleanup-exit.txt","iperf-exit.txt","receiver-exit.txt","flow-exit.txt","flow-settings.txt","flow-phases.tsv","receiver.json")]
        for file in files:
            source=directory/file
            if source.is_file():
                archive.add(source,arcname=str(source.relative_to(root)), recursive=False)
verdict["verified"]=datetime.now(timezone.utc).isoformat()
verdict["status"]="pass" if not verdict["failures"] and not verdict["missing"] and verdict["driver"] and verdict["driver"]["status"]==0 and {(v.split()[0], v.split()[1]) for v in verdict["sources"]} == {("7d7f552333cf0aa6fa9563577b97adbda779eff4", tool) for tool in ("tcp_proxy","nsjoin")} and len(verdict["passed"])==24 else "incomplete"
(public/"verdict.json").write_text(json.dumps(verdict,indent=2)+"\n")
print(json.dumps({k:v if k!="passed" else list(v) for k,v in verdict.items()},indent=2))
raise SystemExit(int(verdict["status"]!="pass"))
