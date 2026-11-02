"""Report the two competing senders over the same portion of a transfer."""
from collections import Counter, defaultdict
from decimal import Decimal
import csv
import gzip
import json
import os
import re
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
    features = (results/"interface-features.txt").read_text()
    assert "client2.0" in features
    for setting in ("tcp_sack", "tcp_timestamps", "tcp_recovery"):
        values = re.findall(rf"net\.ipv4\.{setting} = (\d+)", features)
        assert values == ["1"] * 3, (setting, values)
    variant = root.name.split("-", 1)[1]
    recovery = 1 if variant == "on" else 0
    assert variant in ("on", "off"), variant
    config = (results/"primary/stderr.txt").read_text()
    assert f"WANCONFIG recovery={recovery} send=4194304 receive=4194304" in config
    assert f" rack={str(bool(recovery)).lower()} " in config
    assert (results/"ping-exit.txt").read_text().strip() in ("0","1")
    ping_phases = read_table(results/"ping-phases.tsv")
    assert [p["phase"] for p in ping_phases] == ["probe-launch","flows-begin","flows-end","probe-stop"]
    def hashes(lines):
        return {Path(path).name: sha for line in lines if len(fields := line.split()) == 2
                for sha, path in [fields] if len(sha) == 64 and Path(path).name in ("tcp_proxy", "nsjoin")}
    expected = hashes(line for line in (destination / "helpers.sha256").read_text().splitlines()
                      if "/shared/" in line)
    assert set(expected) == {"tcp_proxy", "nsjoin"}
    assert hashes((results / "settings.txt").read_text().splitlines()) == expected
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
        assert [r["result"] for r in read_table(p/"iperf-attempts.tsv")] == ["0"]
        assert t["proxy-listener-ready"] <= t["both-proxies-ready"] <= t["client-operation-begin"]
        lo,hi = t["client-operation-begin"]-.01,t["client-operation-end"]+.01-dur
        assert 0 <= hi-lo < 4,(lo,hi)
        sides.append((p,stack,lo,hi,dur,intervals,residual))
    A=max(s[3] for s in sides)+1
    B=min(s[2]+s[4] for s in sides)-1
    assert B-A>35
    rates={}
    sockets={}
    complete_rates={}
    for p,stack,lo,hi,dur,intervals,residual in sides:
        received = read_json(p / "receiver.json")["end"]["sum_received"]
        longest_zero = current_zero = 0
        for interval in intervals:
            current_zero = current_zero + interval["seconds"] if interval["bytes"] == 0 else 0
            longest_zero = max(longest_zero, current_zero)
        complete_rates[stack] = {"max_zero_delivery_interval_seconds": longest_zero, "bytes": received["bytes"], "seconds": dur,
                                 "mbps": received["bytes"] * 8 / dur / 1e6}
        for filename in ("receiver.json", "flow-phases.tsv", "iperf-attempts.tsv"):
            (public / f"{p.name}-{filename}").write_bytes((p / filename).read_bytes())
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
            assert all(v["bufsize"] == 4194304 and v["sack_negotiated"] and v["timestamps_negotiated"] for v in r)
            state["send_buffer_bytes"] = 4194304
            state["local_recovery_option"] = recovery
            state.update(peer_headroom_at_most_one_MSS=sum(v["wnd"]-v["seq_flight"]<=v["mss"] for v in r),outstanding_at_or_above_cwnd=sum(v["out"]>=v["cwnd"] for v in r),buffered_beyond_seq_flight=sum(v["queued"]>v["seq_flight"] for v in r),recovery_samples=sum(v["recovery"] for v in r))
        else:
            assert all(v["sndbuf"] == v["rcvbuf"] == 4194304 and v["cc"] == "cubic" and v["tcp_options"] & 3 == 3 for v in r)
            state["send_buffer_bytes"] = state["receive_buffer_bytes"] = 4194304
            state["negotiated_timestamps_sack"] = True
            for key in ("rwnd_us","sndbuf_us","busy_us","retrans"):
                state[key+"_delta"]=r[-1][key]-r[0][key]
            state["unsent_at_least_one_MSS"]=sum(v["notsent"]>=v["mss"] for v in r)
        state["client_port"]=r[0]["port"]
        sockets[stack]=state
        # Exact samples, limited to the experiment's synthetic, active flow.
        dest=public/f"{mode}-{case}-{stack}-samples.jsonl"
        full_flow = [v for v in rows if v["stack"] == stack and v["port"] == r[0]["port"]]
        dest.write_text("".join(json.dumps(v)+"\n" for v in full_flow))
    g,l=rates["netstack"],rates["linux"]
    return dict(overlap_seconds=B-A, local_netstack_recovery=recovery,receiver_rates=complete_rates,mbps_bounds=rates,netstack_share_bounds=[g[0]/(g[0]+l[1]),g[1]/(g[1]+l[0])],sockets=sockets)

CAUSES = ("tlp_confirmation", "rack_reorder_timer", "dupack_scoreboard", "rack_ack", "rto")


def project(folder, public):
    """Preserve unfiltered event records and essential setup even on a failed gate."""
    raw = folder / "results"
    records = []
    for name in ("primary", "secondary"):
        path = raw / name / "stderr.txt"
        if not path.exists():
            continue
        config = []
        for line in path.read_text().splitlines():
            if "WANLOSS " in line:
                records.append(line.split("WANLOSS ", 1)[1])
            if "WANCONFIG " in line:
                config.append(line.split("WANCONFIG ", 1)[1])
        (public / f"{name}-wanconfig.txt").write_text("\n".join(config) + "\n")
        settings = raw / name / "flow-settings.txt"
        if settings.exists():
            (public / f"{name}-flow-settings.txt").write_bytes(settings.read_bytes())
    (public / "all-decisions.jsonl").write_text("".join(line + "\n" for line in records))
    return [json.loads(line) for line in records]


def reconcile(wire, data, events, public, recovery):
    selected = wire["netstack"]["client_port"]
    assert data["sockets"]["netstack"]["client_port"] == selected
    full = {}
    ack_receipts = defaultdict(list)
    for stack, dev in (("netstack","client.1"), ("linux","client2.1")):
        flow = wire[stack]
        actual = flow["client_port"], flow["wan_port"]
        first = None
        with gzip.open(public / f"measured-cubic-netstack-{dev}-packets.tsv.gz", "rt") as f:
            for row in csv.DictReader(f, delimiter="\t"):
                ports = int(row["tcp.srcport"]), int(row["tcp.dstport"])
                when = int(Decimal(row["frame.time_epoch"])*1_000_000_000)
                if first is None and ports == actual and int(row["tcp.len"]) == flow["wire_mss"]:
                    first = when
                if stack == "netstack" and ports == actual[::-1] and int(row["tcp.flags"], 0) & 16 and row["tcp.options.timestamp.tsval"]:
                    left = row["tcp.options.sack_le"].split(",") if row["tcp.options.sack_le"] else []
                    right = row["tcp.options.sack_re"].split(",") if row["tcp.options.sack_re"] else []
                    assert len(left) == len(right)
                    key = (int(row["tcp.ack_raw"]), int(row["tcp.options.timestamp.tsval"]), tuple((int(a),int(b)) for a,b in zip(left,right)))
                    ack_receipts[key].append(when)
        assert first is not None
        full[stack] = first
    selected_events = [dict(e) for e in events if e["peer"]==44000 and e["port"]==selected]
    assert [e["ordinal"] for e in selected_events] == list(range(1,len(selected_events)+1))
    assert all(a["ns"] <= b["ns"] for a,b in zip(selected_events,selected_events[1:]))
    counts = Counter()
    for e in selected_events:
        cause = e["cause"]
        assert cause in CAUSES and (recovery or cause not in {"rack_ack","rack_reorder_timer","tlp_confirmation"})
        assert e["cwnd"] >= 1 and e["cwnd_after"] >= 1
        assert (e["marked"] > 0) == (cause in {"rack_ack","rack_reorder_timer"})
        counts[cause] += 1
        if cause in {"rack_ack","dupack_scoreboard","tlp_confirmation"}:
            key=(e["ack"],e["ack_tsval"],tuple((b["Start"],b["End"]) for b in e["ack_sacks"] or []))
            matches=[t for t in ack_receipts.get(key,()) if t <= e["ns"]]
            e["capture_ack_candidates_before"] = len(matches)
            e["latest_matching_ack_before_us"] = (e["ns"]-max(matches))/1000 if matches else None
        else:
            assert "ack" not in e
        e["since_first_full_ms"] = (e["ns"] - full["netstack"])/1e6
    (public / "payload-decisions.jsonl").write_text("".join(json.dumps(e)+"\n" for e in selected_events))
    return {"counts":{c:counts[c] for c in CAUSES}, "events":len(selected_events),
            "first":selected_events[0] if selected_events else None,
            "full_mss_ns":full, "full_mss_order":sorted(full,key=full.get),
            "ack_wire_matches":sum(e.get("capture_ack_candidates_before",0)>0 for e in selected_events),
            "ack_events":sum("ack" in e for e in selected_events)}

root=Path(os.environ["RUNNER_TEMP"])/"qualification/current-shared"
destination=Path(os.environ["RUNNER_TEMP"])/"tcp-public"
destination.mkdir(exist_ok=True)
heads=(destination/"source-heads").read_text().splitlines() if (destination/"source-heads").exists() else []
verdict={"heads":heads,"baseline":heads[0] if heads else None,"harness":os.environ["GITHUB_SHA"],"passed":{},"failures":{},"missing":[]}
expected=[(f"primary{pair}-{arm}", "cubic") for pair in (1,2) for arm in ("on","off")]
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
        events=project(folder, public)
        wire=capture(folder/"results",public,"measured-"+experiment)
        d=trial(root/arm,"measured",experiment)
        d["wire"]=wire
        d["decisions"]=reconcile(wire,d,events,public,arm.endswith("-on"))
        d["start"]=(folder/"start").read_text().strip()
        d["end"]=(folder/"finish").read_text().strip()
        d["exit"]=int((folder/"exit.txt").read_text().strip())
        for p in ("qdisc-before.txt","qdisc-after.txt","interface-features.txt"):
            (public/(cc+"-"+p)).write_bytes((folder/"results"/p).read_bytes())
        ordered = sorted(d["wire"], key=lambda stack: d["wire"][stack]["first_original_seconds"])
        first_data = [d["wire"][stack]["first_original_seconds"] for stack in ordered]
        d["first_data_order"] = ordered
        d["first_data_gap_s"] = first_data[1] - first_data[0]
        want = "netstack" if arm.startswith("primary") else "linux"
        d["order_verified"] = ordered[0] == want and d["decisions"]["full_mss_order"][0] == want
        qdisc = {}
        for phase in ("before","after"):
            content = (folder / "results" / f"qdisc-{phase}.txt").read_text()
            for dev in ("wan.0", "wan.1"):
                chunk = content.split(dev + "\n", 1)[1].split("\nwan.",1)[0]
                assert "limit 100 delay 50ms rate 20Mbit" in chunk
                qdisc[phase,dev] = int(re.search(r"\(dropped (\d+),", chunk)[1])
        g,l = (d["receiver_rates"][stack]["mbps"] for stack in ("netstack", "linux"))
        d["whole_transfer_jain_fairness"] = (g+l)**2/(2*(g*g+l*l))
        d["qdisc_drops"] = {dev:qdisc["after",dev]-qdisc["before",dev] for dev in ("wan.0","wan.1")}
        verdict["passed"][name]=d
    except (AssertionError,KeyError,ValueError,IndexError,OSError,subprocess.SubprocessError) as exc:
        verdict["failures"][name]={"reason":repr(exc),"diagnostics":diagnostics(folder)}
verdict["driver"]=read_json(root/"completion.json") if (root/"completion.json").exists() else None
verdict["scope"] = "Two fixed gVisor-first RACK on/off pairs on master plus a fork-only decision observer. Linux CUBIC is the common competitor. No inference about observer overhead or uninstrumented event counts."
verdict["status"]="pass" if len(verdict["passed"])==len(expected) and verdict["driver"] and verdict["driver"]["status"]==0 and all(t["order_verified"] for t in verdict["passed"].values()) else "incomplete"
verdict["verified_at"] = datetime.now(timezone.utc).isoformat()
(destination/"verdict.json").write_text(json.dumps(verdict,indent=2)+"\n")
if verdict["status"]!="pass":raise SystemExit(1)
