"""Compare the same Linux keepalive fixtures and benchmarks on two exact sources."""

import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import time

import netip_wildcard as h
import previous


BASE = "975fb172241c7946a9f67b4f52e0349d9240589a"
PRODUCT = "2808320e8f34fa53053acb2d9c443c06a5d3a311"
WORK = Path(os.environ["RUNNER_TEMP"]) / "keepalive"
OUT = WORK / "receipt"
E2E = "//pkg/tcpip/transport/tcp/test/e2e:tcp_test"
BENCH = h.BENCH
NOGO = "//pkg/tcpip/transport/tcp:tcp_nogo"
BENCHSTAT = "@org_golang_x_perf//cmd/benchstat:benchstat"
PACKET = "TestKeepaliveAllocationWire"
SOURCE = Path("pkg/tcpip/transport/tcp/test/e2e/tcp_migration_test.go")
HANDSHAKE = Path("pkg/tcpip/transport/tcp/test/benchmarks/netstack_bench_test.go")
OLD_CHECK = h.check_case
CONTROL = ["TestKeepalive", "TestKeepaliveWithUserTimeout", "TestHandshakeTimeoutConnectedCount", PACKET]
WIRE = {"before_connect_deadline", "after_connect_deadline", "accepted_deadline", "untouched_restore_deadline",
        "rearmed_restore_deadline", "reenabled_deadline"}
REPORT = {"base": BASE, "product": PRODUCT, "machine": os.uname().machine, "errors": {}, "builds": {},
          "arms": {}, "order": [], "inputs": {}, "verified": False}


def save():
    (OUT / "result.json").write_text(json.dumps(REPORT, indent=2) + "\n")


def once(stage, func, *args):
    try:
        return func(*args)
    except Exception as error:
        REPORT["errors"][stage] = repr(error)[:2400]
    finally:
        save()


def git(*args):
    return subprocess.check_output(["git", *args])


def overlay(arm, rev):
    git("checkout", "-q", "--detach", rev)
    assert git("rev-parse", "HEAD").decode().strip() == rev
    assert git("diff", "HEAD", "--") == b""
    src = SOURCE.read_text()
    for old, new in [('	"gvisor.dev/gvisor/pkg/state"', '	"gvisor.dev/gvisor/pkg/buffer"'),
                     ('	"gvisor.dev/gvisor/pkg/tcpip/header"', '	"gvisor.dev/gvisor/pkg/tcpip/faketime"')]:
        assert src.count(old) == 1 and new not in src
        src = src.replace(old, new + "\n" + old)
    SOURCE.write_text(src + "\n" + (WORK / "keepalive_probe.go").read_text())
    HANDSHAKE.write_bytes(HANDSHAKE.read_bytes() + (WORK / "handshake.go.inc").read_bytes())
    info = REPORT["arms"][arm] = {"source": rev, "overlays": {str(f): h.sha(f.read_bytes()) for f in (SOURCE, HANDSHAKE)}}
    if arm == "fixed":
        assert info["overlays"] == REPORT["arms"]["stock"]["overlays"]


def check_case(owner, text, xml, row):
    if owner == NOGO:
        sections = re.split(r"(?m)^(amd64|arm64)\.\.\.", text)
        row["sections"] = {arch: body.strip().splitlines() for arch, body in zip(sections[1::2], sections[2::2])}
        assert len(sections) == 5 and set(row["sections"]) == {"amd64", "arm64"}
        assert all("PASS" in body for body in row["sections"].values())
        assert not any(x.tag in ("error", "failure") for x in xml.iter())
        return
    OLD_CHECK(owner, text, xml, row)
    values = re.findall(r"KEEPALIVE_WIRE (\w+)=(true|false)\s*$", text, re.M)
    row["wire"] = dict(values)
    assert len(values) == len(WIRE) and set(row["wire"]) == WIRE
    assert all(row["wire"][n] == "true" for n in ("accepted_deadline", "after_connect_deadline", "rearmed_restore_deadline"))
    for sub in ("BeforeConnect", "Accepted", "DisableReenable", "Restore"):
        fullname = PACKET + "/" + sub
        assert len(re.findall(r"(?m)^\s*--- PASS: " + re.escape(fullname) + r" \(", text)) == 1
        cases = [el for el in xml.iter("testcase") if el.get("name") == fullname]
        assert len(cases) == 1 and not any(el.tag in ("error", "failure", "skipped") for el in cases[0].iter())


def tests(arm, kind, owner, flags, minutes):
    dest = OUT / (arm + "-" + kind)
    dest.mkdir()
    h.WORK, h.OUT = WORK, dest
    h.GO[E2E] = CONTROL
    h.check_case = check_case
    h.REPORT = {"commands": {}, "tests": {}}
    info = REPORT["arms"][arm].setdefault("tests", {})
    info[kind] = h.REPORT
    h.run(arm + "-" + kind, [owner], flags, minutes)
    assert set(h.REPORT["tests"]) == {owner} and h.REPORT["tests"][owner]["verified"], h.REPORT["tests"]
    assert all(row["exit"] == 0 or row.get("recovered") for row in h.REPORT["commands"].values())


def defaults(name, labels, minutes):
    # Resolve the bytes through targetCompleted's own default group; copy before switching arms.
    stop = time.monotonic() + minutes * 60
    builds = REPORT["builds"][name] = []
    for attempt in range(24):
        bep = WORK / (name + "-" + str(attempt) + ".bep")
        options = ["--config=x86_64", "--lockfile_mode=off", "--jobs=4", "--remote_executor=", "--remote_cache=",
                   "--bes_backend=", "--spawn_strategy=processwrapper-sandbox,local", "--strategy=GoStdlib=processwrapper-sandbox",
                   "--strategy=GoStdlibList=processwrapper-sandbox", "--keep_going", "--nobuild_event_json_file_path_conversion",
                   "--build_event_json_file=" + str(bep)]
        cmd = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g", "build", *options,
               *["--distdir=" + str(d) for d in sorted(previous.DIRS)], *labels]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=max(1, stop - time.monotonic()))
        log = p.stdout.decode(errors="replace")
        row = {"exit": p.returncode, "errors": [x[:600] for x in log.splitlines() if x.startswith("ERROR:")][-12:]}
        builds.append(row)
        save()
        if p.returncode:
            if time.monotonic() < stop and previous.recover(log):
                continue
            raise RuntimeError((name, row))
        events = [json.loads(line) for line in bep.read_bytes().splitlines()]
        sets = {e["id"]["namedSet"]["id"]: e["namedSetOfFiles"] for e in events if "namedSetOfFiles" in e}
        row["invocation"], = [e["started"]["uuid"] for e in events if "started" in e]
        row["defaults"] = {}
        for label, output in labels.items():
            candidates = [(i, e["completed"]) for e in events if "completed" in e and "targetCompleted" in e["id"]
                          for i in [e["id"]["targetCompleted"]] if not i.get("aspect") and
                          (i["label"] == label or (label == BENCHSTAT and "org_golang_x_perf" in i["label"]
                                                   and i["label"].endswith("//cmd/benchstat:benchstat")))]
            (ident, complete), = candidates
            assert complete["success"] and ident["configuration"]["id"]
            group, = (g for g in complete["outputGroup"] if g["name"] == "default")
            pending = [s["id"] for s in group["fileSets"]]
            files = {}
            while pending:
                node = sets[pending.pop()]
                files.update((f["uri"], f) for f in node.get("files", []))
                pending.extend(s["id"] for s in node.get("fileSets", []))
            item, = files.values()
            path, data = h.contents(item, 180 << 20)
            assert data[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<H", data, 18)[0] == 62
            target = WORK / output
            shutil.copyfile(path, target)
            target.chmod(0o755)
            assert h.sha(target.read_bytes()) == h.sha(data)
            row["defaults"][label] = {"label": ident["label"], "config": ident["configuration"]["id"], "bytes": len(data),
                                      "hash": h.sha(data), "filename": path.name, "staged_as": output}
        save()
        return
    raise ValueError("dependency recovery budget exhausted")


def reporters():
    # This is the existing diagnostic-only go_repository block, added after the SUT builds.
    pinned = git("show", "2b51abee8530385431b7225198fdd5cd65e78696:MODULE.bazel").decode()
    block = pinned[pinned.index("# Diagnostic-only reporting tool;"):pinned.index('bazel_dep(name = "rules_go"')]
    assert block.count("go_repository(") == 2 and 'name = "org_golang_x_perf"' in block
    manifest = Path("MODULE.bazel")
    data = manifest.read_text()
    assert 'name = "org_golang_x_perf"' not in data and 'bazel_dep(name = "package_metadata"' not in data
    meta, = [line for line in pinned.splitlines() if line.startswith('bazel_dep(name = "package_metadata"')]
    manifest.write_text(data + "\n" + block + meta + "\n")
    defaults("benchstat", {BENCHSTAT: "benchstat"}, 9)


def measure():
    original = os.sched_getaffinity(0)
    cpus = sorted(original)[:2]
    assert len(cpus) == 2
    REPORT["cpu_ids"], REPORT["cpu_model"] = cpus, next(x.split(":", 1)[1].strip() for x in Path("/proc/cpuinfo").read_text().splitlines() if x.startswith("model name"))
    wanted = {
        "wildcard": ("WildcardBindClose", 12000, ["BenchmarkWildcardBindClose"]),
        "handshake": ("KeepaliveHandshake", 48, ["BenchmarkKeepaliveHandshake/enabled=false", "BenchmarkKeepaliveHandshake/enabled=true"]),
    }
    for arm in ("stock", "fixed"):
        (OUT / (arm + ".bench")).write_text("")
        for mode in wanted:
            assert (WORK / (arm + "-" + mode)).is_file(), (arm, mode)
    os.sched_setaffinity(0, cpus)
    try:
        for block in range(13):
            for arm in (("stock", "fixed") if block % 2 == 0 else ("fixed", "stock")):
                for kind, (name, n, names) in wanted.items():
                    binary = WORK / (arm + "-" + kind)
                    args = [str(binary), "-test.run=^$", "-test.bench=^Benchmark" + name + "$", "-test.benchtime=" + str(n) + "x",
                            "-test.count=1", "-test.benchmem", "-test.cpu=2"]
                    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=50,
                                       env={**os.environ, "GOMAXPROCS":"2"})
                    output = p.stdout.decode(errors="replace")
                    raw = OUT / ("block-%02d-%s-%s.log" % (block, arm, kind))
                    raw.write_bytes(p.stdout)
                    rows = re.findall(r"(?m)^(Benchmark\S+)\s+(\d+)\s+([\d.]+)\s+ns/op\s+([\d.]+)\s+B/op\s+([\d.]+)\s+allocs/op\s*$", output)
                    assert p.returncode == 0 and re.search(r"(?m)^PASS$", output), (raw.name, p.returncode)
                    assert [x[0].removesuffix("-2") for x in rows] == names and all(int(x[1]) == n for x in rows), (raw.name, rows)
                    REPORT["order"].append({"block":block, "arm":arm, "kind":kind, "iterations":n, "original":raw.name})
                    if block:
                        with (OUT / (arm + ".bench")).open("a") as f:
                            for line in output.splitlines():
                                if line.startswith("Benchmark") and "ns/op" in line:
                                    f.write(line + "\n")
                    save()
    finally:
        os.sched_setaffinity(0, original)
    for arm in ("stock", "fixed"):
        assert len((OUT / (arm + ".bench")).read_text().splitlines()) == 36


def analyze():
    output = subprocess.check_output([str(WORK / "benchstat"), "stock=stock.bench", "fixed=fixed.bench"], cwd=OUT, text=True)
    (OUT / "benchstat.txt").write_text(output)
    assert all(name in output for name in ("WildcardBindClose", "KeepaliveHandshake/enabled=false", "KeepaliveHandshake/enabled=true",
                                            "sec/op", "B/op", "allocs/op", "n=12"))


def main():
    assert os.uname().machine == "x86_64" and os.geteuid() != 0
    previous.OUT = WORK
    previous.REPORT["assets"] = REPORT["inputs"]
    for arm, rev in (("stock", BASE), ("fixed", PRODUCT)):
        overlay(arm, rev)
        once(arm + "-defaults", defaults, arm, {E2E: arm + "-wildcard", BENCH: arm + "-handshake"}, 34 if arm == "stock" else 9)
        once(arm + "-wire", tests, arm, "wire", E2E,
             ["--test_arg=-test.v", "--test_env=GO_TEST_WRAP_TESTV=1", "--test_filter=^(" + "|".join(CONTROL) + ")$"], 6)
        if arm == "fixed":
            once("fixed-nogo", tests, arm, "nogo", NOGO, [], 18)
        git("restore", "--", str(SOURCE), str(HANDSHAKE))
    def compare_controls():
        x, y = [REPORT["arms"][arm]["tests"]["wire"]["tests"][E2E]["wire"] for arm in ("stock", "fixed")]
        assert x == y, (x,y)
    once("wire-comparison", compare_controls)
    once("samples", measure)
    once("benchstat-build", reporters)
    if "samples" not in REPORT["errors"] and "benchstat-build" not in REPORT["errors"]:
        once("benchstat-output", analyze)
    REPORT["verified"] = not REPORT["errors"]
    save()
    return 0 if REPORT["verified"] else 1


if __name__ == "__main__":
    try:
        sys_exit = main()
    except Exception as error:
        REPORT["errors"]["setup"] = repr(error)[:1700]
        sys_exit = 1
    finally:
        save()
    raise SystemExit(sys_exit)
