"""Observe one SACK-on bulk socket, with bounded independent packet evidence."""

from decimal import Decimal
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

from tcp_mss_forwarder import BASE, FIXTURE, PRIVATE, PUBLIC, command, diagnostic, put, require, source


def wire(directory):
    # Runs inside this trial's isolated bridge/namespace. Raw snaplen data
    # never leave the private directory; publish only selected numeric headers.
    processes = {}
    record = {"started": time.time_ns(), "diagnostics": {}}

    def start(kind, expression, cap):
        log = (directory / f"{kind}.log").open("w")
        proc = subprocess.Popen(["tcpdump", "-Z", "root", "-n", "-U", "-s", "128",
                                 "-i", "server.1", "-c", str(cap), "-w",
                                 str(directory / f"{kind}.pcap"), expression],
                                stdout=subprocess.DEVNULL, stderr=log)
        processes[kind] = (proc, log)
        for _ in range(100):
            if "listening on" in (directory / f"{kind}.log").read_text():
                return
            require(proc.poll() is None, "capture failed to start")
            time.sleep(0.01)
        raise RuntimeError("capture readiness timeout")

    flow = "tcp and host 10.0.0.3 and host 10.0.0.2 and port 44000"
    try:
        start("syn", f"{flow} and (tcp[tcpflags] & tcp-syn != 0)", 32)
        (directory / "wire-ready").touch()
        deadline = time.monotonic() + 65
        while not (directory / "wire-stop").exists() and time.monotonic() < deadline:
            trigger = directory / "stalled-port"
            if "stall" not in processes and trigger.is_file():
                port = trigger.read_text().strip()
                if port.isdigit():
                    require(1024 <= int(port) <= 65535, "invalid bulk peer")
                    record.update(port=int(port), trigger_read_ns=time.time_ns())
                    start("stall", f"{flow} and port {int(port)}", 384)
                    record["stall_ready_ns"] = time.time_ns()
            time.sleep(0.05)
    finally:
        for kind, (proc, log) in processes.items():
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
            record["diagnostics"][kind] = {"exit": proc.wait(timeout=5)}
            log.close()
            msg = (directory / f"{kind}.log").read_text()
            record["diagnostics"][kind]["counts"] = re.findall(
                r"(?m)^([0-9]+) packets? (captured|received by filter|dropped by kernel)$", msg)
        record["finished"] = time.time_ns()
        put(directory / "wire.json", record)


FIELDS = ["frame.time_epoch", "ip.src", "ip.dst", "tcp.srcport", "tcp.dstport",
          "tcp.flags", "tcp.seq_raw", "tcp.ack_raw", "tcp.window_size_value",
          "tcp.len", "tcp.options"]


def headers(path):
    argv = ["tshark", "-n", "-r", str(path), "-T", "fields", "-E", "separator=|"]
    for name in FIELDS:
        argv.extend(["-e", name])
    text = command(argv)
    rows = []
    for line in text.splitlines():
        row = line.split("|")
        require(len(row) == len(FIELDS) and all(re.fullmatch(r"[0-9a-fx.:,]*", v) for v in row), "unexpected header data")
        require({ipaddress.IPv4Address(row[i]) for i in (1, 2)} ==
                {ipaddress.IPv4Address("10.0.0.2"), ipaddress.IPv4Address("10.0.0.3")}, "unrelated addresses")
        rows.append(row)
    return rows


def interior(iperf):
    all_rows = [v["sum"] for v in iperf["intervals"]]
    omitted = [v for v in all_rows if v["omitted"]]
    measured = [v for v in all_rows if not v["omitted"]]
    require(len(measured) == 20 and omitted and all(not v["sender"] for v in all_rows), "receiver schedule")
    rows = measured[1:-1]
    # iperf 3.16 resets its clock over -O; the first post-omit row has
    # end-start != seconds. Keep it raw, outside the predeclared interior.
    for v in rows:
        require(v["bytes"] >= 0 and 0.75 <= v["seconds"] <= 1.25 and
                math.isclose(v["end"]-v["start"], v["seconds"], abs_tol=1e-5) and
                math.isclose(8*v["bytes"]/v["seconds"], v["bits_per_second"], rel_tol=1e-5),
                "interior byte/time mismatch")
    require(all(abs(a["end"]-b["start"]) < .001 for a,b in zip(rows,rows[1:])), "noncontiguous interior")
    warm = sum(v["bytes"] for v in omitted)
    idle = longest = 0
    for v in rows:
        idle = 0 if v["bytes"] else idle + 1
        longest = max(longest, idle)
    return {"warmup_bytes": warm, "interior_bytes": sum(v["bytes"] for v in rows),
            "longest_zero_bins": longest, "stall": warm >= 1 << 20 and longest >= 3}


def retain(directory, trial):
    # Each file stands independently even if one other sensor or the parser fails.
    data = trial.setdefault("originals", {})
    errors = trial.setdefault("read_errors", {})
    names = ["iperf.json", "server-writes.json", "client-writes.json",
             "wire.json", "wan.0-qdisc.json", "wan.1-qdisc.json"]
    for name in names:
        path = directory / name
        if path.exists():
            try:
                value = json.loads(path.read_text())
                if name == "iperf.json":
                    value.get("start", {}).pop("cookie", None)
                data[name] = value
            except (OSError, ValueError) as exc:
                errors[name] = str(exc)[:160]
    for bound in ("start","end"):
        path=directory / f"iperf-{bound}-ns"
        if path.exists() and path.read_text().strip().isdigit():
            trial[f"iperf_{bound}_ns"]=int(path.read_text())
    meta = trial.setdefault("wire_headers", {})
    for kind in ("syn", "stall"):
        path = directory / f"{kind}.pcap"
        if path.exists():
            try:
                trial.setdefault("pcap_metadata", {})[kind]={"bytes":path.stat().st_size,"sha256":hashlib.sha256(path.read_bytes()).hexdigest()}
                meta[kind] = headers(path)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                errors[kind] = type(exc).__name__
    stats = directory / "link-stats.json"
    if stats.exists():
        try:
            links = json.loads(stats.read_text())
            trial["link_stats"] = {v["ifname"]:v.get("stats64", v.get("stats", {})) for v in links
                                   if v["ifname"] in ("wan.0","wan.1","server.1","client.1")}
        except (OSError, KeyError, TypeError, ValueError) as exc:
            errors["link-stats.json"] = type(exc).__name__
    # Retain actual link/stack/capture counters, not full command output.
    log = directory / "command.log"
    if log.exists():
        trial["diagnostic"] = diagnostic(log.read_text(errors="replace"))
    for ns in ("client", "server"):
        path = directory / f"{ns}-sysctl.txt"
        if path.exists():
            trial.setdefault("sysctl", {})[ns] = dict(line.split(" = ") for line in path.read_text().splitlines() if line.startswith("net.ipv4.tcp_"))
    for dev in ("server.0", "client.0", "server.1", "client.1", "wan.0", "wan.1"):
        path = directory / f"{dev}-features.txt"
        if path.exists():
            trial.setdefault("offloads", {})[dev] = dict(re.findall(
                r"(?m)^(tcp-segmentation-offload|generic-segmentation-offload|generic-receive-offload): (on|off)",path.read_text()))


def validate(trial):
    data = trial["originals"]
    server = data["server-writes.json"]
    iperf = data["iperf.json"]
    require(trial["exit"] == 0 and "error" not in iperf and not trial["read_errors"], "original transfer or sensors incomplete")
    require(iperf["start"]["test_start"]["reverse"] == 1 and iperf["start"]["test_start"]["blksize"] == 61440, "traffic direction/size")
    require(iperf["start"]["test_start"]["duration"] == 20 and iperf["start"]["test_start"]["omit"] == 3, "duration/warmup")
    stack = server["stack"]["in"]
    require(stack["cc"]=="cubic" and stack["sack"] and stack["recovery"]==1 and
            server["buffer"]==61440 and server["num_cpu"] == server["gomaxprocs"] == 2 and
            server["gso_max"] == 0 and not server["gro"] and not server["swgso"], "sender configuration")
    require(len(trial["offloads"])==6 and all(len(v)==3 and set(v.values())=={"off"} for v in trial["offloads"].values()), "actual offloads")
    require(len(trial["sysctl"])==2 and all(v=={"net.ipv4.tcp_sack":"1","net.ipv4.tcp_timestamps":"0","net.ipv4.tcp_congestion_control":"cubic"} for v in trial["sysctl"].values()),"Linux peer settings")
    require(all(len(data[d+"-qdisc.json"])==1 and data[d+"-qdisc.json"][0]["kind"]=="netem" for d in ("wan.0","wan.1")), "qdiscs")
    writers=[w for w in server["writes"] if w["to"]=="*gonet.TCPConn" and sum(int(n)*v for n,v in w["accepted"].items())>=1<<20]
    require(len(writers)==1 and writers[0]["samples"] and writers[0]["accepted"].get("61440",0)>0, "unique sampled bulk stream")
    bulk=writers[0]; port=int(bulk["remote"].split(":")[1])
    require(bulk["local"]=="10.0.0.3:44000" and bulk["remote"]==f"10.0.0.2:{port}", "bulk identity")
    samples=[s for s in bulk["samples"] if s["Sender"]["Why"]!="no_sender"]
    require(samples, "no live bulk socket sample")
    require(all(s["Sender"]["Local"]==44000 and s["Sender"]["Remote"]==port and s["Sender"]["Sack"] and not s["Sender"]["Timestamps"] for s in samples), "sender sample association")
    wire_meta=data["wire.json"]
    syn=trial["wire_headers"].get("syn",[])
    hs=[r for r in syn if {r[3],r[4]}=={"44000",str(port)}]
    require(any(r[1]=="10.0.0.2" and (int(r[5],16)&0x3f)==2 for r in hs) and any(r[1]=="10.0.0.3" and (int(r[5],16)&0x3f)==18 for r in hs), "wire handshake missing")
    require(wire_meta["diagnostics"]["syn"]["exit"]==0, "wire observer failed")
    if "stall" in wire_meta["diagnostics"]:
        require(wire_meta["port"]==port and wire_meta["diagnostics"]["stall"]["exit"]==0 and
                all({r[3],r[4]}=={"44000",str(port)} for r in trial["wire_headers"]["stall"]),"wrong trigger")
    events=[e for e in (server["events"] or []) if (e["Local"],e["Remote"])==(44000,port)]
    rto=next((e for e in events if e["Why"]=="rto_before"),None)
    traffic=trial["wire_headers"].get("stall", [])
    def rxscale(r):
        data=bytes.fromhex(r[-1].replace(":",""))
        while data:
            kind=data[0]
            if kind==0: return None
            length=1 if kind==1 else data[1]
            require(0<length<=len(data),"bad TCP option")
            if kind==3 and length==3: return data[2]
            data=data[length:]
        return None
    scales={r[1]:rxscale(r) for r in hs}
    wire_times=[int(Decimal(r[0])*1_000_000_000) for r in traffic]
    diag=wire_meta["diagnostics"]
    return {"port":port, "bulk_samples":len(samples), "bulk_decisions":len(events),
            "first_retained_bulk_rto_ns":rto and rto["At"],
            "first_rto_known": rto is not None and (not server["events_dropped"] or rto in server["events"][:512]),
            "wire_scope":"SYNs throughout; other headers only after the no-return trigger",
            "negotiated_scale_offers":scales, "sender_scale_readback":samples[0]["Sender"]["Scale"],
            "posttrigger_frames_before_after_rto":([sum(t<rto["At"] for t in wire_times),sum(t>=rto["At"] for t in wire_times)] if rto else None),
            "capture_counts":{k:{label:int(n) for n,label in v["counts"]} for k,v in diag.items()},
            "qdisc_drops":{d:data[d+"-qdisc.json"][0].get("drops") for d in ("wan.0","wan.1")},
            "events_discarded":server["events_dropped"], "stall_capture_triggered":"port" in wire_meta,
            "post_trigger_frame_count":len(trial["wire_headers"].get("stall", [])),
            "interpretation":"Receiver stall is independent of mechanism. Attribute only using the actual time-covered bulk decisions, packet headers and socket/copy state; absence from a truncated stream is not evidence."}


def attempts():
    cpus = sorted(os.sched_getaffinity(0))[:2]
    require(len(cpus)==2, "two CPUs")
    row = {"source": source(), "production":BASE, "fixture":FIXTURE, "fields":FIELDS, "cpus":cpus,
           "host":command(["uname","-srvm"]), "plan":"SACK-on only; up to 8 attempts; stop on >=1 MiB omitted warmup and >=3 consecutive zero-byte interior receiver bins",
           "verdict":"INCOMPLETE", "trials":[]}
    build=json.loads((PUBLIC/"build.json").read_text())
    require(build["source"]==row["source"] and build["verdict"]=="PASS","compiled diagnostic identity")
    out=PUBLIC/"result.json"
    for i in range(8):
        directory=PRIVATE/f"attempt-{i}"
        directory.mkdir()
        trial={"name":f"attempt-{i}","started_ns":time.time_ns()}
        row["trials"].append(trial);put(out,row)
        args=["sudo","-n","env","GOMAXPROCS=2","RUNNER_TEMP="+os.environ["RUNNER_TEMP"],"taskset","-c",",".join(map(str,cpus)),
              "bash","test/benchmarks/tcp/tcp_benchmark.sh","--helpers",str(PRIVATE/"helpers"),
              "--no-user-ns","--ideal","--cubic","--disable-linux-gso","--disable-linux-gro","--mss-study",str(directory),
              "--mss-duration","--duration","20","--server","--sack"]
        try:
            with (directory/"command.log").open("w") as log:
                trial["exit"]=subprocess.run(args,stdout=log,stderr=subprocess.STDOUT,timeout=100).returncode
        except subprocess.TimeoutExpired:
            trial["timeout"]=True
        trial["finished_ns"]=time.time_ns()
        retain(directory,trial);put(out,row)
        try:
            trial["receiver"]=interior(trial["originals"]["iperf.json"])
            trial["observations"]=validate(trial)
            trial["verified"]=True
        except (KeyError,TypeError,ValueError) as exc:
            trial.update(verified=False,error=str(exc)[:160])
        put(out,row)
        # Don't spend more attempts on a broken observer or after reproducing.
        if not trial["verified"] or trial["receiver"]["stall"]:
            break
    if row["trials"][-1].get("receiver", {}).get("stall"):
        row["verdict"]="STALL_OBSERVED"
    elif len(row["trials"])==8 and all(t["verified"] for t in row["trials"]):
        row["verdict"]="NOT_REPRODUCED"
    row["all_sensor_checks_passed"]=all(t["verified"] for t in row["trials"])
    row["verdict_ns"]=time.time_ns();put(out,row)
    require(row["verdict"]!="INCOMPLETE" and row["all_sensor_checks_passed"],"see original receiver verdict and separate sensor checks")


if __name__ == "__main__":
    if sys.argv[1]=="wire":
        wire(Path(sys.argv[2]))
    else:
        attempts()
