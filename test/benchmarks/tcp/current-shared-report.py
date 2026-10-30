"""Report the two competing senders over the same portion of a transfer."""
import csv
import json
import os
from pathlib import Path
from statistics import median
import subprocess
from flight_capture import capture
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

def trial(root, mode, case):
    d = root / mode / case
    cc, primary = case.split("-")
    assert (code := (d/"exit.txt").read_text().strip()) == "0", f"benchmark exit={code}"
    results = d/"results"
    assert (results/"topology-cleanup-exit.txt").read_text().strip() == "0"
    assert (results/"qdisc-after.txt").read_text().count("qdisc netem ") == 2
    assert "client2.0" in (results/"interface-features.txt").read_text()
    assert (results/"ping-exit.txt").read_text().strip() in ("0","1")
    ping_phases = read_table(results/"ping-phases.tsv")
    assert [p["phase"] for p in ping_phases] == ["probe-launch","flows-begin","flows-end","probe-stop"]
    sides = []
    for side, stack in [("primary",primary), ("secondary","linux" if primary=="netstack" else "netstack")]:
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
        dur = end["seconds"]; assert 42 <= dur <= 60, dur
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
    assert B-A>35
    rates={}
    sockets={}
    for p,stack,lo,hi,dur,intervals,residual in sides:
        lower=sum(i["bytes"] for i in intervals if lo+i["start"]>=A and hi+i["end"]<=B)
        upper=sum(i["bytes"] for i in intervals if hi+i["end"]>A and lo+i["start"]<B)
        rates[stack]=[max(0,lower+min(residual,0))*8/(B-A)/1e6,(upper+max(residual,0))*8/(B-A)/1e6]
        rows=[json.loads(l.split("WANSAMPLE ",1)[1]) for l in (p/"stderr.txt").read_text().splitlines() if "WANSAMPLE " in l]
        if mode != "measured":
            assert not rows
            continue
        by={}
        for s in rows:
            if s["stack"]==stack and A<=s["ns"]/1e9<=B:
                by.setdefault(s["port"],[]).append(s)
        def acked(r):
            if len(r)<2: return 0
            if stack=="netstack": return (r[-1]["una"]-r[0]["una"]) % 2**32
            return r[-1]["acked"]-r[0]["acked"]
        samples=sorted(by.values(), key=acked, reverse=True)
        assert samples, "no WAN socket samples"
        r=samples[0]; n=len(r); progress=acked(r)
        assert n>=20 and progress>1e5 and all(acked(other)<progress*.05 for other in samples[1:]), (stack,n,progress)
        state=dict(samples=n,bytes_acked_during_samples=progress,cwnd_packets_median=median(v["cwnd"] for v in r),window_bytes_median=median(v["wnd"] for v in r),rtt_us_median=median(v["rtt_us"] for v in r),window_zero_samples=sum(v["wnd"]==0 for v in r),observed_ns=[r[0]["ns"],r[-1]["ns"]],max_sample_gap_seconds=max(b["ns"]-a["ns"] for a,b in zip(r,r[1:]))/1e9)
        if stack=="netstack":
            state.update(peer_headroom_at_most_one_MSS=sum(v["wnd"]-v["seq_flight"]<=v["mss"] for v in r),outstanding_at_or_above_cwnd=sum(v["out"]>=v["cwnd"] for v in r),buffered_beyond_seq_flight=sum(v["queued"]>v["seq_flight"] for v in r),recovery_samples=sum(v["recovery"] for v in r))
        else:
            for key in ("rwnd_us","sndbuf_us","busy_us","retrans"):
                state[key+"_delta"]=r[-1][key]-r[0][key]
            state["unsent_at_least_one_MSS"]=sum(v["notsent"]>=v["mss"] for v in r)
        sockets[stack]=state
        # Exact samples, limited to the experiment's synthetic, active flow.
        dest=public/f"{mode}-{case}-{stack}-samples.jsonl"
        full_flow = [v for v in rows if v["stack"] == stack and v["port"] == r[0]["port"]]
        dest.write_text("".join(json.dumps(v)+"\n" for v in full_flow))
    g,l=rates["netstack"],rates["linux"]
    return dict(overlap_seconds=B-A,mbps_bounds=rates,netstack_share_bounds=[g[0]/(g[0]+l[1]),g[1]/(g[1]+l[0])],sockets=sockets)

root=Path(os.environ["RUNNER_TEMP"])/"qualification/current-shared"
destination=Path(os.environ["RUNNER_TEMP"])/"tcp-public"
destination.mkdir(exist_ok=True)
heads=(destination/"source-heads").read_text().splitlines() if (destination/"source-heads").exists() else []
verdict={"heads":heads,"upstream":heads[0] if heads else None,"harness":os.environ["GITHUB_SHA"],"passed":{},"failures":{},"missing":[]}
expected=[(arm,cc) for arm in ("a-combined","b-combined") for cc in ("reno","cubic")]
for arm,cc in expected:
    experiment=cc+"-netstack"
    name=arm+"/measured/"+experiment
    folder=root/name
    if not (folder/"exit.txt").exists():
        verdict["missing"].append(name)
        continue
    public=destination/arm
    public.mkdir(exist_ok=True)
    try:
        d=trial(root/arm,"measured",experiment)
        d["wire"]=capture(folder/"results",public,"measured-"+experiment)
        d["start"]=(folder/"start").read_text().strip()
        d["end"]=(folder/"finish").read_text().strip()
        d["exit"]=int((folder/"exit.txt").read_text().strip())
        for p in ("qdisc-before.txt","qdisc-after.txt","interface-features.txt"):
            (public/(cc+"-"+p)).write_bytes((folder/"results"/p).read_bytes())
        verdict["passed"][name]=d
    except (AssertionError,KeyError,ValueError,IndexError,OSError,subprocess.SubprocessError) as exc:
        verdict["failures"][name]={"reason":repr(exc),"diagnostics":diagnostics(folder)}
verdict["driver"]=read_json(root/"completion.json") if (root/"completion.json").exists() else None
verdict["status"]="pass" if len(verdict["passed"])==len(expected) and verdict["driver"] and verdict["driver"]["status"]==0 else "incomplete"
(destination/"verdict.json").write_text(json.dumps(verdict,indent=2)+"\n")
if verdict["status"]!="pass":raise SystemExit(1)
