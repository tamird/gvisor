import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

base = "f51a205a8f4afbc5ab41aded070df74951b081ea"
change = "902bee9176a0a78a435db3df2ebffe1d2a9794e5"
analyzer = "//pkg/tcpip/transport/tcp:tcp_nogo"
packet = "//pkg/tcpip/transport/tcp/test/e2e:tcp_test"
out = Path(os.environ["RUNNER_TEMP"]) / "keepalive-qualification"
ep = Path("pkg/tcpip/transport/tcp/endpoint.go")
conn = ep.with_name("connect.go")
rec = {"product": change, "base": base, "phases": {}}
fields = ("count", "idle", "interval", "unacked")


def save():
    (out / "results.json").write_text(json.dumps(rec, indent=2) + "\n")


def source(ref):
    subprocess.run(["git", "checkout", "-q", "--detach", ref], check=True)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == ref


def unguard():
    e = ep.read_text()
    for read in (
        "v := e.keepalive.count",
        "*o = tcpip.KeepaliveIdleOption(e.keepalive.idle)",
        "*o = tcpip.KeepaliveIntervalOption(e.keepalive.interval)",
    ):
        guarded = "\t\te.keepalive.Lock()\n\t\t" + read + "\n\t\te.keepalive.Unlock()\n"
        assert e.count(guarded) == 1
        e = e.replace(guarded, "\t\t" + read + "\n")
    ep.write_text(e)
    c = conn.read_text()
    probe = "\te.keepalive.unacked++\n\te.keepalive.Unlock()"
    assert c.count(probe) == 1
    conn.write_text(c.replace(probe, "\te.keepalive.Unlock()\n\te.keepalive.unacked++"))


def run(phase, label, args=()):
    bep = out / (phase + ".bep")
    log = out / (phase + ".stderr")
    r = rec["phases"][phase] = {"owner": label, "tests": [], "arches": {}, "attempts": []}
    save()
    cmd = ["bazel", "test", "--config=x86_64", "-c", "opt", "--jobs=3", "--noshow_progress",
           "--color=no", "--curses=no", "--nocache_test_results", "--flaky_test_attempts=1",
           "--test_sharding_strategy=disabled", "--test_timeout=550", "--build_event_json_file=" + str(bep)]
    with log.open("w") as stream:
        r["exit"] = subprocess.run(cmd + list(args) + [label], stdout=stream, stderr=subprocess.STDOUT).returncode
    r["diagnostics"] = [l[:650] for l in log.read_text(errors="replace").splitlines() if "ERROR:" in l][:8]
    if bep.exists():
        for line in bep.open():
            event = json.loads(line)
            target = event.get("id", {}).get("testResult", {}).get("label")
            if target == label and "testResult" in event:
                t = event["testResult"]
                r["attempts"].append({"status": t.get("status"), "cached": t.get("cachedLocally", False)})
    logdir = Path("bazel-testlogs") / label[2:].replace(":", "/")
    if r["attempts"] and (logdir / "test.log").is_file():
        text = (logdir / "test.log").read_text(errors="replace")
        r["test_log_sha256"] = hashlib.sha256(text.encode()).hexdigest()
        if label == analyzer:
            pieces = re.split(r"(amd64|arm64)\.\.\.", text)
            for i in range(1, len(pieces), 2):
                arch, lines = pieces[i:i + 2]
                r["arches"][arch] = [l[-650:] for l in lines.splitlines() if "checklocks" in l or "keepaliveMutex" in l][:20]
        if label == packet and (logdir / "test.xml").is_file():
            r["tests"] = [{"name": node.get("name"), "result": node.get("result"), "status": node.get("status"), "children": [c.tag for c in node]} for node in ET.parse(logdir / "test.xml").iter("testcase")][:30]
        r["key_lines"] = [l[-650:] for l in text.splitlines() if "keepalive" in l.lower() or re.search(r"arm64|amd64",l)][:35]
    print(phase, json.dumps(r), flush=True)
    save()
    return r


save()
source(change)
positive = run("positive", analyzer)
packets = run("packets", packet, ("--test_filter=^TestKeepalive(WithUserTimeout)?$", "--test_arg=-test.v"))
for ref, name in ((base, "unguarded_base"), (change, "unguarded_proposal")):
    source(ref)
    unguard()
    run(name, analyzer)
    subprocess.run(["git", "restore", "--", ep, conn], check=True)
ordinary = rec["phases"]["unguarded_base"]
negative = rec["phases"]["unguarded_proposal"]
ok = lambda r: r["exit"] == 0 and any(a["status"] == "PASSED" and not a["cached"] for a in r["attempts"])
def matches(lines, field):
    return any(re.search(r"\b" + field + r"\b", line) and "keepaliveMutex" in line for line in lines)
test_names = {c["name"] for c in packets["tests"] if not set(c["children"]) & {"skipped", "failure", "error"}}
rec["verified"] = ok(ordinary) and ok(positive) and ok(packets) and {"TestKeepalive","TestKeepaliveWithUserTimeout"} <= test_names and negative["exit"] != 0 and all(all(matches(negative["arches"].get(arch, []), field) for field in fields) for arch in ("amd64","arm64"))
save()
raise SystemExit(0 if rec["verified"] else 1)
