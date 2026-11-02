"""One fixed Linux reverse-transfer discriminator; no production change."""

import hashlib
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time
import uuid

BASE = "918aefd5f3771a161013fb72873d8aa19196e1bc"
FIXTURE = "b7bb7cc69936f81262603a7c6825f6ccd089b240"
ROOT = Path(os.environ["RUNNER_TEMP"]) / "mss-forwarder"
PRIVATE = ROOT / "private"
PUBLIC = ROOT / "public"
BYTES = 240 << 20
BUFFER = 0xF000
DEVICES = ["client.0", "client.1", "server.0", "server.1", "wan.0", "wan.1", "br0", "br1"]


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def command(args, **kwargs):
    return subprocess.run(args, text=True, check=True, capture_output=True, **kwargs).stdout.strip()


def put(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def source():
    require(command(["git", "rev-parse", "HEAD^"]) == FIXTURE, "fixture source")
    require(command(["git", "rev-parse", "HEAD^^"]) == BASE, "stock source")
    require(not command(["git", "diff", BASE, "--", "pkg", "runsc", "tools", "MODULE.bazel", ".bazelrc"]), "production changed")
    require(not command(["git", "status", "--porcelain", "--untracked-files=no"]), "tracked tree dirty")
    return command(["git", "rev-parse", "HEAD"])


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build():
    PRIVATE.mkdir(parents=True, exist_ok=True)
    PUBLIC.mkdir(exist_ok=True)
    row = {"source": source(), "production": BASE, "fixture": FIXTURE, "verdict": "INCOMPLETE"}
    bazel = ["bazel", f"--bazelrc={ROOT / 'remote.bazelrc'}"]
    flags = ["--compilation_mode=opt", "--jobs=16", "--remote_download_outputs=all"]
    row["invocation"] = str(uuid.uuid4())
    bep = PRIVATE / "build.bep.json"
    flags += [f"--invocation_id={row['invocation']}", f"--build_event_json_file={bep}"]
    start = time.time()
    with (PRIVATE / "build.log").open("w") as log:
        p = subprocess.run(bazel + ["run", *flags, f"--script_path={PRIVATE / 'start'}", "//test/benchmarks/tcp:tcp_benchmark"], stdout=log, stderr=subprocess.STDOUT, timeout=1800)
    row.update(exit=p.returncode, command_start_unix=start, command_finish_unix=time.time())
    put(PUBLIC / "build.json", row)
    require(p.returncode == 0, "build failed; original log retained on runner")
    events = [json.loads(line) for line in bep.read_text().splitlines()]
    completions = [e["completed"] for e in events if e.get("id", {}).get("targetCompleted", {}).get("label") == "//test/benchmarks/tcp:tcp_benchmark"]
    require(len(completions) == 1 and completions[0]["success"], "target completion")
    row["target_completed"] = True
    # The selection query is a separate invocation, not the original build.
    flags = flags[:3]
    expr = 'str(target.label) + "\\t" + target[DefaultInfo].files_to_run.executable.path'
    paths = command(bazel + ["cquery", *flags, "set(//test/benchmarks/tcp:tcp_proxy //test/benchmarks/tcp:nsjoin)", "--output=starlark", "--starlark:expr=" + expr])
    binaries = {}
    helpers = PRIVATE / "helpers"
    helpers.mkdir(exist_ok=True)
    for line in paths.splitlines():
        label, path = line.split("\t")
        name = label.rsplit(":", 1)[1]
        require(name in {"tcp_proxy", "nsjoin"} and name not in binaries, "unexpected executable")
        p = Path(path)
        elf = command(["readelf", "-h", str(p)])
        require("Advanced Micro Devices X86-64" in elf, "non-native executable")
        (helpers / name).write_bytes(p.read_bytes())
        (helpers / name).chmod(0o755)
        binaries[name] = {"path": path, "sha256": digest(p), "bytes": p.stat().st_size}
    require(set(binaries) == {"tcp_proxy", "nsjoin"}, "missing executable")
    row.update(verdict="PASS", binaries=binaries, observed_at_unix=time.time())
    put(PUBLIC / "build.json", row)


def options(raw):
    data = bytes.fromhex(raw.replace(":", ""))
    result = []
    while data:
        kind = data[0]
        result.append(kind)
        if kind == 0:
            break
        size = 1 if kind == 1 else data[1]
        require(0 < size <= len(data), "invalid option encoding")
        data = data[size:]
    return result


def packets(directory, arm):
    fields = ["ip.src", "ip.dst", "tcp.srcport", "tcp.dstport", "tcp.flags", "tcp.len", "tcp.hdr_len", "tcp.options", "ip.len", "tcp.stream", "tcp.options.mss_val", "ip.frag_offset", "ip.flags.mf"]
    argv = ["tshark", "-r", str(directory / "capture.pcap"), "-Y", "tcp", "-T", "fields", "-E", "separator=/t"]
    for field in fields:
        argv.extend(["-e", field])
    result = [dict(zip(fields, line.split("\t"), strict=True)) for line in command(argv).splitlines()]
    require(len(result) == 200, "short independent capture")
    synack = [r for r in result if int(r["tcp.flags"], 16) & 0x12 == 0x12]
    require(synack, "missing handshake")
    for r in synack:
        opts = options(r["tcp.options"])
        require(r["tcp.options.mss_val"] == "1460" and 8 not in opts, "MSS/TS negotiation")
        require((4 in opts) == (arm != "off"), "actual SACK negotiation")
    for r in result:
        require(int(r["ip.len"]) <= 1500, "oversized captured packet")
        require(r["ip.frag_offset"] in {"", "0"} and r["ip.flags.mf"] == "0", "fragmented packet")
    data = [r for r in result if r["ip.src"] == "10.0.0.3" and int(r["tcp.len"]) > 1000]
    require(data, "no sender data")
    return {"interface": "server.1", "packets": result, "large_payload_lengths": sorted({int(r["tcp.len"]) for r in data})}


def observe(directory, arm, amount, capture):
    iperf = json.loads((directory / "iperf.json").read_text())
    require("error" not in iperf, "iperf error")
    sent, received = iperf["end"]["sum_sent"], iperf["end"]["sum_received"]
    require(sent["bytes"] == received["bytes"] == amount, "receiver byte count")
    require(sent["seconds"] > 0 and received["seconds"] > 0, "nonpositive measurement interval")
    write = json.loads((directory / "server-writes.json").read_text())
    require(write["buffer"] == BUFFER and write["gomaxprocs"] == write["num_cpu"] == 2, "buffer/CPU setting")
    require(write["gso_max"] == 0 and not write["swgso"] and not write["gro"], "forwarder offload settings")
    rows = write["writes"]
    total = lambda r: sum(int(n) * count for n, count in r["accepted"].items())
    data = [r for r in rows if total(r) == amount]
    require(len(data) == 1, "unique actual data Write stream")
    data = data[0]
    require(data["to"] == ("*net.TCPConn" if arm == "linux" else "*gonet.TCPConn"), "sender implementation")
    require(data["accepted"] == data["requested"] and data["write_errors"] == 0, "short/failed Write")
    require(data["completed"], "data copy still active at counter snapshot")
    require(data["accepted"].get(str(BUFFER), 0) > 0, "no actual 60KiB Write")
    require(all(0 < int(n) <= BUFFER for n in data["accepted"]), "unexpected Write size")
    if arm != "linux":
        require(write["stack"]["in"]["sack"] == (arm == "on") and write["stack"]["in"]["cc"] == "cubic", "netstack option readback")
    features = {}
    for dev in DEVICES:
        text = (directory / f"{dev}-features.txt").read_text()
        features[dev] = {name: re.search(rf"^{name}: (\w+)", text, re.M).group(1) for name in ("tcp-segmentation-offload", "generic-segmentation-offload", "generic-receive-offload")}
        if dev not in {"br0", "br1"}:
            require(set(features[dev].values()) == {"off"}, "offload remains enabled")
    links = {}
    for ns in ("client", "server", "root"):
        for link in json.loads((directory / f"{ns}-links.json").read_text()):
            if link["ifname"] in DEVICES:
                links[link["ifname"]] = link["mtu"]
    require(set(links) == set(DEVICES) and set(links.values()) == {1500}, "actual link MTUs")
    controls = {}
    for ns in ("client", "server"):
        controls[ns] = dict(line.split(" = ") for line in (directory / f"{ns}-sysctl.txt").read_text().splitlines())
        require(controls[ns] == {"net.ipv4.tcp_timestamps": "0", "net.ipv4.tcp_sack": "1", "net.ipv4.tcp_congestion_control": "cubic"}, "native peer settings")
    qdiscs = {dev: json.loads((directory / f"{dev}-qdisc.json").read_text()) for dev in ("wan.0", "wan.1")}
    result = {"sent": sent, "received": received, "sender_writes": data, "stack": write["stack"], "links": links, "features": features, "sysctl": controls, "qdiscs": qdiscs}
    if capture:
        result["capture"] = packets(directory, arm)
    return result


def run():
    row = {"source": source(), "production": BASE, "fixture": FIXTURE, "verdict": "INCOMPLETE", "trials": []}
    build_row = json.loads((PUBLIC / "build.json").read_text())
    require(build_row["verdict"] == "PASS" and build_row["source"] == row["source"], "build source")
    cpus = sorted(os.sched_getaffinity(0))[:2]
    require(len(cpus) == 2, "two available CPUs")
    row.update(cpus=cpus, host=command(["uname", "-srvm"]), iperf_version=command(["iperf3", "--version"]), build=build_row)
    schedule = [("capture-" + arm, arm, 32 * BUFFER, True) for arm in ("linux", "off", "on")]
    schedule.append(("linux-start", "linux", BYTES, False))
    for pair in range(4):
        for arm in (("on", "off") if pair % 2 == 0 else ("off", "on")):
            schedule.append((f"pair-{pair}-{arm}", arm, BYTES, False))
    schedule.append(("linux-end", "linux", BYTES, False))
    for name, arm, amount, capture in schedule:
        directory = PRIVATE / name
        directory.mkdir(exist_ok=True)
        trial = {"name": name, "arm": arm, "capture_control": capture, "bytes": amount, "verdict": "INCOMPLETE", "start_unix": time.time()}
        row["trials"].append(trial)
        argv = ["sudo", "-n", "env", "GOMAXPROCS=2", "taskset", "-c", ",".join(map(str, cpus)), "bash", "test/benchmarks/tcp/tcp_benchmark.sh", "--helpers", str(PRIVATE / "helpers"), "--no-user-ns", "--ideal", "--cubic", "--disable-linux-gso", "--disable-linux-gro", "--mss-study", str(directory), "--mss-bytes", str(amount)]
        if arm != "linux":
            argv.append("--server")
        if arm != "off":
            argv.append("--sack")
        if capture:
            argv.append("--mss-capture")
        try:
            with (directory / "command.log").open("w") as log:
                p = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, timeout=100)
            trial.update(exit=p.returncode, finish_unix=time.time())
            require(p.returncode == 0, "transfer command failed")
            trial.update(observe(directory, arm, amount, capture))
            trial["verdict"] = "PASS"
        except Exception as exc:
            trial["error"] = type(exc).__name__ + ": " + str(exc)[:160]
        put(PUBLIC / "result.json", row)
    require(source() == row["source"], "source changed during run")
    require(len(row["trials"]) == 13, "incomplete schedule")
    for name, binary in build_row["binaries"].items():
        require(digest(PRIVATE / "helpers" / name) == binary["sha256"], "executable changed")
    if all(t["verdict"] == "PASS" for t in row["trials"]):
        row["verdict"] = "PASS"
        row["receiver_mbps_medians"] = {arm: statistics.median(t["received"]["bytes"] * 8 / t["received"]["seconds"] / 1e6 for t in row["trials"] if not t["capture_control"] and t["arm"] == arm) for arm in ("linux", "on", "off")}
    row["verdict_unix"] = time.time()
    put(PUBLIC / "result.json", row)
    require(row["verdict"] == "PASS", "one or more original trials did not verify")


def scan():
    require(PUBLIC.is_dir(), "no evidence directory")
    for p in PUBLIC.iterdir():
        require(p.name in {"build.json", "result.json"} and p.is_file() and not p.is_symlink(), "unexpected output")
        content = p.read_bytes()
        require(len(content) <= 2_000_000, "oversized report")
        for name in ("BUILDBUDDY_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"):
            key = os.environ.get(name, "")
            require(not key or key.encode() not in content, "credential in output")
        require(not re.search(rb"(?i)authorization|x-buildbuddy-api-key|gh[pousr]_[A-Za-z0-9]{20}", content), "credential marker")


if __name__ == "__main__":
    {"build": build, "run": run, "scan": scan}[sys.argv[1]]()
