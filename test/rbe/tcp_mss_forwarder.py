"""One fixed Linux reverse-transfer discriminator; no production change."""

import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import struct
import subprocess
import sys
import time
import uuid
import zipfile

BASE = "918aefd5f3771a161013fb72873d8aa19196e1bc"
FIXTURE = "b7bb7cc69936f81262603a7c6825f6ccd089b240"
ORIGINAL = "83434b081e25e0c1d2f6310b51242ea86ac711b5"
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
    require(command(["git", "rev-parse", "HEAD^"]) == ORIGINAL, "original carrier")
    require(command(["git", "rev-parse", "HEAD~3"]) == FIXTURE, "fixture source")
    require(command(["git", "rev-parse", "HEAD~4"]) == BASE, "stock source")
    require(not command(["git", "diff", FIXTURE, "--", "test/benchmarks/tcp/BUILD", "test/benchmarks/tcp/tcp_proxy.go", "test/benchmarks/tcp/write_observation.go"]), "compiled fixture changed")
    require(not command(["git", "diff", BASE, "--", "pkg", "runsc", "tools", "MODULE.bazel", ".bazelrc"]), "production changed")
    require(not command(["git", "status", "--porcelain", "--untracked-files=no"]), "tracked tree dirty")
    return command(["git", "rev-parse", "HEAD"])


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def diagnostic(text):
    """Retain the first compiler/analysis error and bounded causal context."""
    lines = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text).splitlines()
    first = next((i for i, line in enumerate(lines) if re.search(r"ERROR:|error:|compilepkg:", line)), None)
    result = []
    for line in lines[first:first + 36] if first is not None else []:
        if re.search(r"authorization|x-buildbuddy|(?:remote|bes)_header|credential|password|token\s*[:=]|https?://", line, re.I):
            continue
        key = os.environ.get("BUILDBUDDY_API_KEY", "")
        if key and key in line:
            continue
        result.append(line[:1000])
    return {"sha256": hashlib.sha256(text.encode()).hexdigest(), "first_error_context": result}


def build():
    PRIVATE.mkdir(parents=True, exist_ok=True)
    PUBLIC.mkdir(exist_ok=True)
    row = {"source": source(), "production": BASE, "fixture": FIXTURE, "verdict": "INCOMPLETE"}
    # Reuse the native hosted-Linux build from the earlier WAN driver. The
    # original remote worker's GCC headers differed from the client toolchain.
    bazel = ["bazel"]
    flags = ["--compilation_mode=opt", "--jobs=5"]
    row.update(execution="native Linux coordinator", original_run=41430977952,
               original_recovery=41432032243, original_build_exit=1)
    row.update(prior_native_run=41433088932, prior_native_build_exit=0,
               selection={}, binaries={})
    paths = {}
    # Reuse the earlier WAN driver's per-owner output query before compiling.
    for name in ("tcp_proxy", "nsjoin"):
        label = "//test/benchmarks/tcp:" + name
        p = subprocess.run(bazel + ["cquery", *flags, label, "--output=files"],
                           text=True, capture_output=True, timeout=300)
        selected = p.stdout.strip().splitlines()
        row["selection"][name] = {"label": label, "exit": p.returncode,
                                  "diagnostics": diagnostic(p.stderr)}
        if p.returncode == 0:
            row["selection"][name]["paths"] = selected
        put(PUBLIC / "build.json", row)
        require(p.returncode == 0, "output selection failed")
        require(len(selected) == 1 and Path(selected[0]).name == name,
                "unique expected executable output")
        paths[name] = selected[0]
    row["invocation"] = str(uuid.uuid4())
    bep = PRIVATE / "build.bep.json"
    flags += [f"--invocation_id={row['invocation']}", f"--build_event_json_file={bep}"]
    start = time.time()
    with (PRIVATE / "build.log").open("w") as log:
        p = subprocess.run(bazel + ["run", *flags, f"--script_path={PRIVATE / 'start'}", "//test/benchmarks/tcp:tcp_benchmark"], stdout=log, stderr=subprocess.STDOUT, timeout=1800)
    row.update(exit=p.returncode, command_start_unix=start, command_finish_unix=time.time())
    if p.returncode:
        row.update(verdict="FAIL", diagnostics=diagnostic((PRIVATE / "build.log").read_text()))
    put(PUBLIC / "build.json", row)
    require(p.returncode == 0, "build failed; original log retained on runner")
    events = [json.loads(line) for line in bep.read_text().splitlines()]
    completions = [e["completed"] for e in events if e.get("id", {}).get("targetCompleted", {}).get("label") == "//test/benchmarks/tcp:tcp_benchmark"]
    require(len(completions) == 1 and completions[0]["success"], "target completion")
    row["target_completed"] = True
    binaries = row["binaries"]
    helpers = PRIVATE / "helpers"
    helpers.mkdir(exist_ok=True)
    for name, path in paths.items():
        p = Path(path)
        require(p.is_file() and os.access(p, os.X_OK), "built executable missing")
        elf = command(["readelf", "-h", str(p)])
        require("Advanced Micro Devices X86-64" in elf, "non-native executable")
        (helpers / name).write_bytes(p.read_bytes())
        (helpers / name).chmod(0o755)
        binaries[name] = {"path": path, "sha256": digest(p), "bytes": p.stat().st_size}
        put(PUBLIC / "build.json", row)
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


def unfragmented(row):
    return int(row["ip.frag_offset"], 0) == 0 and row["ip.flags.mf"].lower() in {"0", "false"}


def packets(directory, arm, record):
    fields = ["ip.src", "ip.dst", "tcp.srcport", "tcp.dstport", "tcp.flags", "tcp.len", "tcp.hdr_len", "tcp.options", "ip.len", "tcp.stream", "tcp.options.mss_val", "ip.frag_offset", "ip.flags.mf"]
    argv = ["tshark", "-r", str(directory / "capture.pcap"), "-Y", "tcp", "-T", "fields", "-E", "separator=/t"]
    for field in fields:
        argv.extend(["-e", field])
    result = [dict(zip(fields, line.split("\t"), strict=True)) for line in command(argv).splitlines()]
    # Retain the original decode before any observation can reject it.
    record.update(interface="server.1", packets=result,
                  pcap_sha256=digest(directory / "capture.pcap"),
                  pcap_bytes=(directory / "capture.pcap").stat().st_size)
    require(len(result) == 200, "short independent capture")
    syn = [r for r in result if int(r["tcp.flags"], 16) & 0x12 == 0x02]
    require(syn, "missing client SYN")
    for r in syn:
        opts = options(r["tcp.options"])
        require(r["tcp.options.mss_val"] == "1460" and 8 not in opts and 4 in opts,
                "native client MSS/TS/SACK negotiation")
    synack = [r for r in result if int(r["tcp.flags"], 16) & 0x12 == 0x12]
    require(synack, "missing handshake")
    for r in synack:
        opts = options(r["tcp.options"])
        require(r["tcp.options.mss_val"] == "1460" and 8 not in opts, "MSS/TS negotiation")
        require((4 in opts) == (arm != "off"), "actual SACK negotiation")
    for r in result:
        require(int(r["ip.len"]) <= 1500, "oversized captured packet")
        require(unfragmented(r), "fragmented packet")
    data = [r for r in result if r["ip.src"] == "10.0.0.3" and int(r["tcp.len"]) > 1000]
    require(data, "no sender data")
    record["large_payload_lengths"] = sorted({int(r["tcp.len"]) for r in data})


def observe(directory, arm, amount):
    iperf = json.loads((directory / "iperf.json").read_text())
    require("error" not in iperf, "iperf error")
    sent, received = iperf["end"]["sum_sent"], iperf["end"]["sum_received"]
    if amount is not None:
        require(sent["bytes"] == received["bytes"] == amount, "receiver byte count")
    else:
        require(iperf["start"]["test_start"]["reverse"] == 1 and iperf["start"]["test_start"]["blksize"] == BUFFER, "reverse duration sender")
        require(iperf["start"]["test_start"]["duration"] == 20 and iperf["start"]["test_start"]["omit"] == 3, "duration/warmup settings")
    require(sent["seconds"] > 0 and received["seconds"] > 0, "nonpositive measurement interval")
    write = json.loads((directory / "server-writes.json").read_text())
    require(write["buffer"] == BUFFER and write["gomaxprocs"] == write["num_cpu"] == 2, "buffer/CPU setting")
    require(write["gso_max"] == 0 and not write["swgso"] and not write["gro"], "forwarder offload settings")
    rows = write["writes"]
    total = lambda r: sum(int(n) * count for n, count in r["accepted"].items())
    implementation = "*net.TCPConn" if arm == "linux" else "*gonet.TCPConn"
    data = [r for r in rows if total(r) == amount] if amount is not None else [r for r in rows if r["to"] == implementation and total(r) > (1 << 20)]
    require(len(data) == 1, "unique actual data Write stream")
    data = data[0]
    require(data["to"] == ("*net.TCPConn" if arm == "linux" else "*gonet.TCPConn"), "sender implementation")
    if amount is not None:
        require(data["accepted"] == data["requested"] and data["write_errors"] == 0, "short/failed Write")
        require(data["completed"], "data copy still active at counter snapshot")
    else:
        # Duration termination can close the socket during the final Write.
        # Keep all errors/partial writes; these histograms include warmup and
        # are not the receiver's measured byte count.
        require(total(data) >= received["bytes"], "accepted bytes below delivered measurement")
    require(data["accepted"].get(str(BUFFER), 0) > 0, "no actual 60KiB Write")
    require(all((0 if amount is None else 1) <= int(n) <= BUFFER for n in data["accepted"]), "unexpected Write size")
    if arm != "linux":
        require(write["stack"]["in"]["sack"] == (arm == "on") and write["stack"]["in"]["cc"] == "cubic" and write["stack"]["in"]["recovery"] == 1, "netstack option readback")
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
    if amount is None:
        intervals = [entry["sum"] for entry in iperf["intervals"]]
        measured = [entry for entry in intervals if entry["omitted"] is False]
        require(any(entry["omitted"] is True for entry in intervals), "no observed omitted warmup")
        require(len(measured) >= 18 and all(entry["sender"] is False for entry in intervals), "receiver interval selection")
        for entry in measured:
            require(entry["bytes"] >= 0 and math.isfinite(entry["seconds"]) and entry["seconds"] > 0, "invalid receiver interval")
            require(abs(entry["end"] - entry["start"] - entry["seconds"]) < 0.00001, "interval duration")
            require(math.isclose(entry["bits_per_second"], entry["bytes"] * 8 / entry["seconds"], rel_tol=0.00001), "interval rate/byte identity")
        require(all(abs(a["end"] - b["start"]) < 0.001 for a, b in zip(measured, measured[1:])), "noncontiguous measured intervals")
        interior = measured[1:-1]
        seconds = sum(entry["seconds"] for entry in interior)
        delivered = sum(entry["bytes"] for entry in interior)
        require(seconds >= 15 and delivered <= received["bytes"], "interior receiver coverage")
        result.update(receiver_intervals=intervals,
                      steady={"selection": "non-omitted receiver intervals excluding first and last", "interval_count": len(interior), "start": interior[0]["start"], "end": interior[-1]["end"], "bytes": delivered, "seconds": seconds, "mbps": delivered * 8 / seconds / 1e6},
                      write_histogram_scope="entire warmup and transfer, including termination")
    return result


def run(recovery=False):
    row = {"source": source(), "production": BASE, "fixture": FIXTURE, "verdict": "INCOMPLETE", "trials": []}
    build_row = json.loads((PUBLIC / "build.json").read_text())
    require(build_row["verdict"] == "PASS" and build_row["source"] == (ORIGINAL if recovery else row["source"]), "build source")
    cpus = sorted(os.sched_getaffinity(0))[:2]
    require(len(cpus) == 2, "two available CPUs")
    row.update(cpus=cpus, host=command(["uname", "-srvm"]), iperf_version=command(["iperf3", "--version"]), build=build_row)
    schedule = [("capture-" + arm, arm, 32 * BUFFER, True) for arm in ("linux", "off", "on")]
    if recovery:
        row["original"] = json.loads((PRIVATE / "original.json").read_text())
        row["parser_preflight"] = json.loads((PRIVATE / "parser.json").read_text())
        row["comparison_plan"] = {"pairs": 8, "on_first": 4, "off_first": 4, "duration_seconds": 20, "omitted_seconds": 3, "selection": "interior non-omitted receiver intervals", "native_controls": ["start", "end"]}
    prefix = "steady-" if recovery else ""
    amount = None if recovery else BYTES
    schedule.append((prefix + "linux-start", "linux", amount, False))
    for pair in range(8 if recovery else 4):
        for arm in (("on", "off") if pair % 2 == 0 else ("off", "on")):
            schedule.append((f"{prefix}pair-{pair}-{arm}", arm, amount, False))
    schedule.append((prefix + "linux-end", "linux", amount, False))
    for name, arm, amount, capture in schedule:
        directory = PRIVATE / name
        directory.mkdir(exist_ok=True)
        trial = {"name": name, "arm": arm, "capture_control": capture, "bytes": amount, "verdict": "INCOMPLETE", "start_unix": time.time()}
        row["trials"].append(trial)
        argv = ["sudo", "-n", "env", "GOMAXPROCS=2", "taskset", "-c", ",".join(map(str, cpus)), "bash", "test/benchmarks/tcp/tcp_benchmark.sh", "--helpers", str(PRIVATE / "helpers"), "--no-user-ns", "--ideal", "--cubic", "--disable-linux-gso", "--disable-linux-gro", "--mss-study", str(directory)]
        argv.extend(["--mss-duration", "--duration", "20"] if amount is None else ["--mss-bytes", str(amount)])
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
            trial.update(observe(directory, arm, amount))
            if capture:
                trial["capture"] = {}
                packets(directory, arm, trial["capture"])
            trial["verdict"] = "PASS"
        except Exception as exc:
            trial["error"] = type(exc).__name__ + ": " + str(exc)[:160]
            if (directory / "command.log").is_file():
                trial["command_diagnostic"] = diagnostic((directory / "command.log").read_text(errors="replace"))
            # Preserve completed measurements even if a later observation gate
            # rejects them. This does not turn an incomplete trial into PASS.
            trial["retained"] = {}
            for filename in ("iperf.json", "server-writes.json"):
                path = directory / filename
                if path.is_file() and path.stat().st_size <= 100_000:
                    try:
                        value = json.loads(path.read_text())
                        if filename == "iperf.json":
                            value = {key: value.get(key) for key in ("start", "intervals", "end", "error")}
                        trial["retained"][filename] = value
                    except ValueError:
                        trial["retained"][filename] = {"sha256": digest(path), "parse": "invalid JSON"}
        put(PUBLIC / "result.json", row)
    require(source() == row["source"], "source changed during run")
    require(len(row["trials"]) == (21 if recovery else 13), "incomplete schedule")
    for name, binary in build_row["binaries"].items():
        require(digest(PRIVATE / "helpers" / name) == binary["sha256"], "executable changed")
    row["capture_verdict"] = "PASS" if all(t["verdict"] == "PASS" for t in row["trials"] if t["capture_control"]) else "INCOMPLETE"
    row["comparison_verdict"] = "PASS" if all(t["verdict"] == "PASS" for t in row["trials"] if not t["capture_control"]) else "INCOMPLETE"
    if recovery and row["comparison_verdict"] == "PASS":
        row["steady_receiver_mbps_medians"] = {arm: statistics.median(t["steady"]["mbps"] for t in row["trials"] if not t["capture_control"] and t["arm"] == arm) for arm in ("linux", "on", "off")}
        row["paired_on_over_off"] = []
        for pair in range(8):
            selected = {t["arm"]: t for t in row["trials"] if t["name"].startswith(f"steady-pair-{pair}-")}
            row["paired_on_over_off"].append({"pair": pair, "order": [arm for arm in selected], "ratio": selected["on"]["steady"]["mbps"] / selected["off"]["steady"]["mbps"]})
    if all(t["verdict"] == "PASS" for t in row["trials"]):
        row["verdict"] = "PASS"
        if not recovery:
            row["receiver_mbps_medians"] = {arm: statistics.median(t["received"]["bytes"] * 8 / t["received"]["seconds"] / 1e6 for t in row["trials"] if not t["capture_control"] and t["arm"] == arm) for arm in ("linux", "on", "off")}
    row["verdict_unix"] = time.time()
    put(PUBLIC / "result.json", row)
    require(row["verdict"] == "PASS", "one or more original trials did not verify")



def recover():
    """Reuse original binaries for captures and fixed-duration comparisons."""
    PRIVATE.mkdir(parents=True, exist_ok=True)
    PUBLIC.mkdir(exist_ok=True)
    source()
    result_zip = ROOT / "original.zip"
    helper_zip = ROOT / "helpers.zip"
    require(digest(result_zip) == "72a38b0c7aadc599c74af019e7d53c42bd6893a66fa6a6e97b450cb4739e3e79", "original archive")
    require(digest(helper_zip) == "a2f08e06cfbee8bb1da7d9da5da4a366b710fd493299cd50305590eb554489f1", "original helpers")
    with zipfile.ZipFile(result_zip) as z:
        require(set(z.namelist()) == {"build.json", "result.json"}, "original members")
        build_row = json.loads(z.read("build.json"))
        original = json.loads(z.read("result.json"))
    require(build_row == original["build"] and build_row["source"] == ORIGINAL and build_row["verdict"] == "PASS", "original built source")
    require(original["source"] == ORIGINAL and original["production"] == BASE and original["fixture"] == FIXTURE, "original measurement source")
    require(len(original["trials"]) == 13 and sum(t["verdict"] == "PASS" for t in original["trials"]) == 9, "original dispositions")
    require(next(t for t in original["trials"] if t["name"] == "pair-1-on")["exit"] == 1, "original refusal")
    put(PRIVATE / "original.json", original)
    put(PUBLIC / "build.json", build_row)
    helpers = PRIVATE / "helpers"
    helpers.mkdir(exist_ok=True)
    with zipfile.ZipFile(helper_zip) as z:
        require(set(z.namelist()) == {"public/build.json", "private/helpers/nsjoin", "private/helpers/tcp_proxy"}, "helper members")
        require(json.loads(z.read("public/build.json")) == build_row, "helper receipt")
        for name in build_row["binaries"]:
            path = helpers / name
            path.write_bytes(z.read("private/helpers/" + name))
            path.chmod(0o755)
    scan_helpers()
    # A known Ethernet/IPv4/TCP SYN-ACK proves the installed tshark's Boolean
    # representation before any new capture. No compiler or network is needed.
    frame = bytes.fromhex("0200000000020200000000010800")
    frame += struct.pack("!BBHHHBBH4s4s", 0x45, 0, 40, 1, 0x4000, 64, 6, 0, bytes([10, 0, 0, 3]), bytes([10, 0, 0, 2]))
    frame += struct.pack("!HHIIHHHH", 44000, 32000, 1, 1, 0x5012, 65535, 0, 0)
    path = PRIVATE / "known.pcap"
    path.write_bytes(struct.pack("<IHHIIII", 0xa1b2c3d4, 2, 4, 0, 0, 128, 1) + struct.pack("<IIII", 1, 0, len(frame), len(frame)) + frame)
    fields = ["ip.frag_offset", "ip.flags.mf"]
    text = command(["tshark", "-r", str(path), "-T", "fields", "-e", fields[0], "-e", fields[1]])
    decoded = dict(zip(fields, text.split("\t"), strict=True))
    preflight = {"tshark": command(["tshark", "--version"]).splitlines()[0], "pcap_sha256": digest(path), "decode": decoded, "original_predicate": decoded[fields[0]] in {"", "0"} and decoded[fields[1]] == "0", "corrected_predicate": unfragmented(decoded)}
    put(PRIVATE / "parser.json", preflight)
    put(PUBLIC / "result.json", {"source": source(), "verdict": "INCOMPLETE", "original": original, "parser_preflight": preflight})
    require(preflight["corrected_predicate"], "known unfragmented packet rejected")
    require(not unfragmented({fields[0]: "0", fields[1]: "True"}) and not unfragmented({fields[0]: "1", fields[1]: "False"}), "fragment rejection")
    run(recovery=True)

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


def scan_helpers():
    scan()
    row = json.loads((PUBLIC / "build.json").read_text())
    require(row["verdict"] == "PASS", "unverified build outputs")
    files = list((PRIVATE / "helpers").iterdir())
    require({p.name for p in files} == set(row["binaries"]) == {"tcp_proxy", "nsjoin"}, "helper names")
    for p in files:
        require(p.is_file() and not p.is_symlink(), "helper file type")
        record = row["binaries"][p.name]
        require(p.stat().st_size == record["bytes"] <= 64 << 20 and digest(p) == record["sha256"], "helper bytes")
        data = p.read_bytes()
        require(data[:6] == b"\x7fELF\x02\x01" and data[18:20] == b"\x3e\x00", "AMD64 ELF")
        for name in ("BUILDBUDDY_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"):
            key = os.environ.get(name, "")
            require(not key or key.encode() not in data, "credential in executable")


if __name__ == "__main__":
    {"build": build, "run": run, "scan": scan, "scan_helpers": scan_helpers, "recover": recover}[sys.argv[1]]()
