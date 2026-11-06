"""Bind one GoogleTest to the native and two source-matched Sentry results."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET


ROOT = Path(os.environ["RUNNER_TEMP"]) / "packet-reserve"
OUT = ROOT / "evidence"
FIX = "5de0b08c436460cd3f68cf9a3dfc0bef8f891407"
BASE = "f51a205a8f4afbc5ab41aded070df74951b081ea"
LLVM = "04238eea04899c21015c4abdac19509e1adfd916"
CASE = "PacketMmapTest.PacketReserve"
TEST = "test/syscalls/linux/packet_mmap.cc"
PRODUCT = ("pkg/sentry/socket/netstack/netstack.go", "pkg/tcpip/tcpip.go",
           "pkg/tcpip/transport/packet/endpoint.go")
PAYLOAD = "//test/syscalls/linux:packet_mmap_test"
NATIVE = "//test/syscalls:packet_mmap_test_native"
SENTRY = "//test/syscalls:packet_mmap_test_runsc_systrap_directfs"
OPTIONS = ["--config=x86_64", "--jobs=3", "--local_resources=memory=9900",
           "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
           "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox",
           "--remote_executor=", "--remote_cache=", "--bes_backend=", "--spawn_strategy=linux-sandbox",
           "--color=no", "--curses=no", "--noshow_progress"]
report = {"fixed": FIX, "stock": BASE, "case": CASE, "phases": {}, "verified": False}


def save():
    (OUT / "result.json").write_text(json.dumps(report, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def read_result(arm, label, result):
    info = result.get("executionInfo", {})
    data = {"status": result.get("status"), "strategy": info.get("strategy"),
            "cached": bool(result.get("cachedLocally") or info.get("cachedRemotely"))}
    for entry in result.get("testActionOutput", []):
        name = entry["name"]
        uri = urlsplit(entry.get("uri", ""))
        if name not in ("test.log", "test.xml") or uri.scheme != "file":
            continue
        path = Path(unquote(uri.path))
        if not path.is_file():
            continue
        data[name] = {"sha256": digest(path), "bytes": path.stat().st_size}
        text = path.read_text(errors="replace")
        if name == "test.log":
            data["gtest"] = re.findall(r"^\[\s*(OK|FAILED|SKIPPED)\s*\]\s+" + re.escape(CASE) + r"\s+\(", text, re.M)
            data["reserveRead"] = bool(re.search(r"getsockopt\([^\n]*PACKET_RESERVE", text))
            data["errno92"] = bool(re.search(r"Protocol not available|ENOPROTOOPT|errno[:= ]+92", text, re.I))
        else:
            if path.stat().st_size < 65536:
                (OUT / (arm + "-" + label.split(":")[1] + ".xml")).write_bytes(path.read_bytes())
            try:
                cases = list(ET.fromstring(text).iter("testcase"))
            except ET.ParseError:
                continue
            data["xmlCases"] = [
                {"name": c.get("classname", "") + "." + c.get("name", ""), "status": c.get("status"),
                 "result": c.get("result"), "outcomes": [e.tag for e in c if e.tag in ("skipped", "error", "failure")]}
                for c in cases if c.get("status") != "notrun" and c.get("result") != "suppressed"
            ][:32]
    return data


def phase(name, cmd, args):
    row = report["phases"][name] = {"source": git("rev-parse", "HEAD"), "attempts": {}}
    row["sourceBlobs"] = {p: git("hash-object", p) for p in (*PRODUCT, TEST)}
    save()
    bep = ROOT / (name + ".bep")
    console = ROOT / (name + ".console")
    with console.open("w") as stream:
        row["exit"] = subprocess.run([str(ROOT / "bazel"), "--nosystem_rc", "--nohome_rc",
            "--host_jvm_args=-Xmx3000m", cmd, *OPTIONS, "--build_event_json_file=" + str(bep),
            *args], stdout=stream, stderr=subprocess.STDOUT).returncode
    if bep.exists():
        with bep.open() as stream:
            for line in stream:
                e = json.loads(line)
                if "started" in e:
                    row["invocation"] = e["started"]["uuid"]
                label = e["id"].get("testResult", {}).get("label")
                if label in (NATIVE, SENTRY) and "testResult" in e:
                    row["attempts"].setdefault(label, []).append(read_result(name, label, e["testResult"]))
    text = console.read_text(errors="replace")
    row["diagnostics"] = [s for s in ("absolute path inclusion", "no such file or directory",
        "undefined reference", "permission denied", "operation not permitted", "no space left",
        "signal: killed", "unrecognized", "no matching", "exception specifier", "timed out") if s in text.lower()]
    row["errors"] = [l[:350] for l in text.splitlines() if l.startswith("ERROR:") or "error:" in l][:6]
    save()
    print(name, row["exit"], row["diagnostics"], flush=True)
    return row


def runtime(arm, labels):
    row = phase(arm, "test", ["--keep_going", "--strategy=TestRunner=local", "--nocache_test_results",
        "--flaky_test_attempts=1", "--test_sharding_strategy=disabled", "--test_timeout=360",
        "--build_tag_filters=", "--test_tag_filters=", "--test_output=all", "--test_env=GTEST_FILTER="+CASE,
        """--run_under=/usr/bin/unshare --user --map-root-user --net /bin/sh -ec '/sbin/ip link set lo up; exec "$@"' packet-test""",
        *labels])
    paths = ["test/syscalls/linux/packet_mmap_test", "release/runsc", "release/gvisor-bin/gvisor_sentry"]
    row["binaries"] = {}
    for rel in paths:
        p = Path("bazel-bin") / rel
        if p.is_file():
            with p.open("rb") as stream:
                header = stream.read(20)
            row["binaries"][p.name] = {"sha256": digest(p), "bytes": p.stat().st_size,
                "amd64ELF": header[:6] == b"\x7fELF\x02\x01" and header[18:20] == b"\x3e\x00"}
    save()
    return row


def observed(row, label, passed):
    records = row["attempts"].get(label, [])
    if len(records) != 1:
        return False
    rec, = records
    xml = rec.get("xmlCases", [])
    if rec.get("strategy") != "local" or rec["cached"] or len(xml) != 1 or xml[0]["name"] != CASE:
        return False
    if passed:
        return rec["status"] == "PASSED" and rec.get("gtest") == ["OK"] and not xml[0]["outcomes"]
    return (rec["status"] == "FAILED" and rec.get("gtest") == ["FAILED"] and "failure" in xml[0]["outcomes"]
            and rec.get("reserveRead") and rec.get("errno92"))


save()
assert git("rev-parse", "HEAD") == FIX and not git("diff", "--name-only", "HEAD")
base = BASE
if phase("gnu-test-and-runtimes", "build", [PAYLOAD, NATIVE, SENTRY])["exit"]:
    git("fetch", "--quiet", "--no-tags", "--depth=1", "https://github.com/tamird/gvisor.git", LLVM)
    assert not git("diff", "--name-only", BASE, LLVM, "--", *PRODUCT, TEST)
    git("checkout", "--quiet", "--detach", LLVM)
    git("restore", "--source=" + FIX, "--", *PRODUCT, TEST)
    assert sorted(git("diff", "--name-only", "HEAD").splitlines()) == sorted((*PRODUCT, TEST))
    base = LLVM
    if phase("llvm-test-and-runtimes", "build", [PAYLOAD, NATIVE, SENTRY])["exit"]:
        raise SystemExit("Neither configuration built the test")
report["integrationBase"] = base
assert all(git("hash-object", p) == git("rev-parse", FIX + ":" + p) for p in (*PRODUCT, TEST))
fixed = runtime("fixed", [NATIVE, SENTRY])
git("restore", "--source=" + base, "--", *PRODUCT)
assert git("diff", "--name-only", base, "--", "pkg", "test").splitlines() == [TEST]
assert git("hash-object", TEST) == git("rev-parse", FIX + ":" + TEST)
stock = runtime("stock", [SENTRY])
ids = all(set(r["binaries"]) == {"packet_mmap_test", "runsc", "gvisor_sentry"}
          and all(f["amd64ELF"] for f in r["binaries"].values()) for r in (stock, fixed))
report["verified"] = bool(fixed["exit"] == 0 and observed(fixed, NATIVE, True) and observed(fixed, SENTRY, True)
    and stock["exit"] == 3 and observed(stock, SENTRY, False) and ids
    and fixed["binaries"]["packet_mmap_test"] == stock["binaries"]["packet_mmap_test"]
    and fixed["binaries"]["gvisor_sentry"]["sha256"] != stock["binaries"]["gvisor_sentry"]["sha256"])
save()
raise SystemExit(0 if report["verified"] else 1)
