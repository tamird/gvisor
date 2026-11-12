"""Report the two competing senders over the same portion of a transfer."""
import csv
import json
import gzip
import os
import re
from statistics import median
from pathlib import Path
import subprocess
import shutil
from flight_capture import capture
from timeout_repair import measure
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
    assert (results/"qdisc-after.txt").read_text().count("qdisc netem ") == 4
    features = (results/"interface-features.txt").read_text()
    assert "client2.0" in features
    for setting in ("tcp_sack", "tcp_timestamps", "tcp_recovery"):
        values = re.findall(rf"net\.ipv4\.{setting} = (\d+)", features)
        assert values == ["1"] * 3, (setting, values)
    variant = root.name.split("-")[1]
    recovery = 1
    assert mode == "plain"
    config = (results/"primary/stderr.txt").read_text()
    assert f"WANCONFIG recovery={recovery} send=4194304 receive=4194304" in config
    assert f" rack={str(bool(recovery)).lower()} " in config
    assert (results/"ping-exit.txt").read_text().strip() in ("0","1")
    ping_phases = read_table(results/"ping-phases.tsv")
    assert [p["phase"] for p in ping_phases] == ["probe-launch","flows-begin","flows-end","probe-stop"]
    def hashes(lines):
        return {Path(path).name: sha for line in lines if len(fields := line.split()) == 2
                for sha, path in [fields] if len(sha) == 64 and Path(path).name in ("tcp_proxy", "tcp_native_proxy", "nsjoin")}
    expected = hashes(line for line in (destination / "helpers.sha256").read_text().splitlines()
                      if f"/{variant}/" in line)
    assert set(expected) == {"tcp_proxy","tcp_native_proxy","nsjoin"}
    assert hashes((results / "settings.txt").read_text().splitlines()) == expected
    sides = []
    for side, stack in [("primary",primary), ("secondary","linux" if primary=="netstack" else "netstack")]:
        p = results/side
        for key in ("cleanup","iperf","receiver","flow"):
            assert (p/f"{key}-exit.txt").read_text().strip() == "0", key
        assert f"congestion_control={cc}" in (p/"flow-settings.txt").read_text()
        assert f"client={stack} " in (p/"flow-settings.txt").read_text()
        cmds = (p/"flow-settings.txt").read_text().splitlines()
        client_cmd = next(line for line in cmds if line.startswith("client_command:"))
        server_cmd = next(line for line in cmds if line.startswith("server_command:"))
        assert f"-observation={mode}" in client_cmd
        assert f"/{variant}/tcp_native_proxy " in server_cmd
        assert f"/{variant}/" + ("tcp_proxy " if stack=="netstack" else "tcp_native_proxy ") in client_cmd
        log = (p/"stderr.txt").read_text()
        assert "WANSAMPLE " not in log and "WANLOSS " not in log
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
    g,l=rates["netstack"],rates["linux"]
    native = re.findall(r"WANCONFIG_NATIVE port=(\d+) peer=44001 send=4194304 receive=4194304 cc=cubic", (results/"secondary/stderr.txt").read_text())
    assert native, "no native socket configuration"
    for side in ("primary","secondary"):
        (public/f"{side}-flow-settings.txt").write_bytes((results/side/"flow-settings.txt").read_bytes())
    return dict(native_config_ports=[int(x) for x in native],helper_sha256=expected,overlap_seconds=B-A, local_netstack_recovery=recovery,receiver_rates=complete_rates,mbps_bounds=rates,netstack_share_bounds=[g[0]/(g[0]+l[1]),g[1]/(g[1]+l[0])])

root=Path(os.environ["RUNNER_TEMP"])/"qualification/current-shared"
destination=Path(os.environ["RUNNER_TEMP"])/"tcp-public"
destination.mkdir(exist_ok=True)
heads=(destination/"source-heads").read_text().splitlines() if (destination/"source-heads").exists() else []
verdict={"heads":heads,"baseline":heads[0] if heads else None,"harness":os.environ["GITHUB_SHA"],"passed":{},"failures":{},"missing":[]}
expected=[(f"primary{pair}-{arm}", "cubic") for pair in range(1, 4) for arm in ("reference", "accounting", "corrected")]
for arm,cc in expected:
    experiment=cc+"-netstack"
    name=arm+"/plain/"+experiment
    folder=root/name
    if not (folder/"exit.txt").exists():
        verdict["missing"].append(name)
        continue
    public=destination/arm
    public.mkdir(exist_ok=True)
    for side in ("primary","secondary"):
        for fn in ("receiver.json","flow-phases.tsv","flow-settings.txt","iperf-attempts.tsv","flow-exit.txt"):
            src=folder/"results"/side/fn
            if src.is_file():
                (public/(side+"-"+fn)).write_bytes(src.read_bytes())
        log=folder/"results"/side/"stderr.txt"
        if log.is_file():
            cfg=[line for line in log.read_text(errors="replace").splitlines() if "WANCONFIG" in line or " rack=" in line]
            (public/(side+"-socket-config.txt")).write_text("\n".join(cfg)+"\n")
            if side == "primary":
                with log.open() as src, gzip.open(public/"sender-ack-decisions.txt.gz", "wt") as dst:
                    for line in src:
                        if "WANACK " in line:
                            dst.write(line[line.index("WANACK "):])
    for dev in ("client.1", "server.1"):
        pcap = folder/"results"/f"{dev}.pcap"
        if pcap.is_file():
            shutil.copyfile(pcap, public/pcap.name)
    for fn in ("qdisc-before.txt","qdisc-after.txt","interface-features.txt","settings.txt","client.1-capture.txt","client2.1-capture.txt","server.1-capture.txt", "ping.txt", "ping-phases.tsv", "queue-samples.txt"):
        src=folder/"results"/fn
        if src.is_file():
            (public/fn).write_bytes(src.read_bytes())
    try:
        events = []
        pattern = re.compile(r"WANRTO ns=(\d+) port=(\d+) una=(\d+) nxt=(\d+)")
        for line in (folder/"results/primary/stderr.txt").read_text().splitlines():
            if "WANRTO " in line:
                match = pattern.search(line)
                assert match, line
                events.append(dict(zip(("ns", "port", "una", "nxt"), map(int, match.groups()))))
        (public/"retransmission-timeouts.json").write_text(json.dumps(events) + "\n")
        wire=capture(folder/"results",public,"plain-"+experiment)
        d=trial(root/arm,"plain",experiment)
        d["wire"]=wire
        d["repairs"] = measure(public, events, wire["netstack"]["client_port"])
        with gzip.open(public/"sender-ack-decisions.txt.gz", "rt") as decisions:
            rows = sum(f' port={wire["netstack"]["client_port"]} ' in line for line in decisions)
        d["advancing_sack_decisions"] = rows
        assert d["wire"]["linux"]["client_port"] in d["native_config_ports"]
        d["start"]=(folder/"start").read_text().strip()
        d["end"]=(folder/"finish").read_text().strip()
        d["exit"]=int((folder/"exit.txt").read_text().strip())
        for p in ("qdisc-before.txt","qdisc-after.txt","interface-features.txt"):
            (public/(cc+"-"+p)).write_bytes((folder/"results"/p).read_bytes())
        ordered = sorted(d["wire"], key=lambda stack: d["wire"][stack]["first_full_seconds"])
        first_data = [d["wire"][stack]["first_full_seconds"] for stack in ordered]
        d["first_data_order"] = ordered
        d["first_data_gap_s"] = first_data[1] - first_data[0]
        want = "netstack" if arm.startswith("primary") else "linux"
        d["order_verified"] = ordered[0] == want and first_data[0] < first_data[1]
        layout = "combined"
        qdisc = {}
        devices = ("wan.0","prop.0","prop.1","wan.1")
        # wan.0 -> prop.0 forwards payload; prop.1 -> wan.1 returns ACKs.
        for phase in ("before","after"):
            content = (folder / "results" / f"qdisc-{phase}.txt").read_text()
            parts = re.split(r"^(wan[.][01]|prop[.][01])\n", content, flags=re.M)
            assert parts[1::2] == list(devices), parts[1::2]
            for dev,chunk in zip(parts[1::2],parts[2::2]):
                limit = 100 if dev in ("wan.0","prop.1") else 4096
                assert f"limit {limit} " in chunk.splitlines()[0]+" ", chunk
                assert ("rate 20Mbit" in chunk) == (limit == 100), chunk
                assert ("delay 50ms" in chunk) == ((layout=="combined") == (limit==100)), chunk
                qdisc[phase,dev] = int(re.search(r"\(dropped (\d+),", chunk)[1])
        times = read_table(folder / "results/ping-phases.tsv")
        stop = float(next(x["unix_seconds"] for x in times if x["phase"]=="flows-begin"))
        ping = (folder / "results/ping.txt").read_text()
        times_ms = [float(rtt) for t,rtt in re.findall(r"^\[([\d.]+)\].*? time=([\d.]+) ms",ping,re.M) if float(t) < stop]
        assert len(times_ms) >= 30 and 95 <= median(times_ms) <= 110, times_ms
        d["unloaded_ping"] = {"count":len(times_ms),"median_ms":median(times_ms),"min_ms":min(times_ms),"max_ms":max(times_ms)}
        samples = (folder / "results/queue-samples.txt").read_text()
        observed = {dev:[] for dev in devices}
        for row in samples.split("qdisc netem ")[1:]:
            header = row.splitlines()[0].split()
            dev = header[header.index("dev")+1]
            if dev not in observed: continue
            matches = re.search(r"backlog [^ ]+ (\d+)p",row.partition("qdisc ")[0])
            if matches: observed[dev].append(int(matches[1]))
        assert min(map(len,observed.values())) >= 100, {dev:len(values) for dev,values in observed.items()}
        d["sampled_queue"] = {dev:{"count":len(values),"max_packets":max(values)} for dev,values in observed.items()}
        g,l = (d["receiver_rates"][stack]["mbps"] for stack in ("netstack", "linux"))
        d["whole_transfer_jain_fairness"] = (g+l)**2/(2*(g*g+l*l))
        d["qdisc_drops"] = {dev:qdisc["after",dev]-qdisc["before",dev] for dev in devices}
        assert all(v>=0 for v in d["qdisc_drops"].values())
        assert all(d["qdisc_drops"][dev]==0 for dev in ("prop.0","wan.1")), d["qdisc_drops"]
        verdict["passed"][name]=d
    except (AssertionError,KeyError,StopIteration,ValueError,IndexError,OSError,subprocess.SubprocessError) as exc:
        verdict["failures"][name]={"reason":repr(exc),"diagnostics":diagnostics(folder)}
verdict["driver"]=read_json(root/"completion.json") if (root/"completion.json").exists() else None
verdict["scope"] = "All three gVisor senders carry the complete published #16419 and share the same sender and queue observations. The corrected arm enables the per-ACK allowance in addition to its copy-based flight estimate. Linux CUBIC competes on the same combined 100-packet queue for every 45-second trial."
orders = [("reference", "accounting", "corrected"), ("accounting", "corrected", "reference"), ("corrected", "reference", "accounting")]
schedule = [f"primary{pair}-{arm} cubic primary" for pair, order in enumerate(orders, 1) for arm in order]
verdict["schedule"] = (destination / "schedule.txt").read_text().splitlines() if (destination / "schedule.txt").is_file() else []
verdict["actual_start_order"] = sorted(verdict["passed"], key=lambda name: verdict["passed"][name]["start"])
results = list(verdict["passed"].values())
verdict["status"] = "incomplete"
if (len(verdict["passed"]) == 9 and verdict["schedule"] == schedule
        and verdict["actual_start_order"] == [line.split()[0] + "/plain/cubic-netstack" for line in schedule]
        and verdict["driver"] and verdict["driver"]["status"] == 0
        and all(r["order_verified"] and r["wire"]["netstack"]["horizon_s"] > 40 for r in results)
        and max(r["unloaded_ping"]["median_ms"] for r in results) - min(r["unloaded_ping"]["median_ms"] for r in results) <= 3):
    verdict["status"] = "pass"
    for variant in ("reference", "accounting", "corrected"):
        with (destination / (variant + ".bench")).open("w") as out:
            for pair in range(1, 4):
                result = verdict["passed"][f"primary{pair}-{variant}/plain/cubic-netstack"]
                rate = result["receiver_rates"]["netstack"]["mbps"] * 1e6 / 8
                timing = result["wire"]["netstack"]
                gap = max(timing["interior_gap_s"], timing["terminal_gap_s"])
                out.write(f"BenchmarkSharedLinkCUBIC 1 {rate:.6f} B/s {gap:.6f} sec/max-new-data-gap\n")
verdict["verified_at"] = datetime.now(timezone.utc).isoformat()
(destination/"verdict.json").write_text(json.dumps(verdict,indent=2)+"\n")
if verdict["status"]!="pass":raise SystemExit(1)
