"""Run the public owners unchanged, then the instrumented two-flight experiment."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit

tmp = Path(os.environ["RUNNER_TEMP"])
out = tmp / "extra-rto"
product = "a848809a530e40d0556b02c8f028189c343f1350"
probe = "5ec985cac60647a369c94e7f6386693bbd6363c5"
report = {"product": product, "probe": probe, "phases": {}}


def save():
    (out / "result.json").write_text(json.dumps(report, indent=2))


def run(name, source, owners, test_filter=None):
    subprocess.run(["git", "checkout", "--detach", source], check=True)
    sender_blob = subprocess.check_output(["git", "hash-object", "pkg/tcpip/transport/tcp/snd.go"], text=True).strip()
    assert sender_blob == subprocess.check_output(["git", "rev-parse", product+":pkg/tcpip/transport/tcp/snd.go"], text=True).strip()
    phase = {"source": source, "sender": sender_blob, "owners": {}}
    report["phases"][name] = phase
    save()
    bep = out / (name + ".bep")
    cmd = [str(tmp/"bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g", "test",
           "--config=x86_64", "--jobs=3", "--strategy=TestRunner=local", "--test_env=GO_TEST_WRAP_TESTV=1",
           "--test_sharding_strategy=disabled", "--nocache_test_results", "--flaky_test_attempts=1",
           "--test_timeout=200", "--keep_going", "--build_event_json_file="+str(bep)]
    if test_filter:
        cmd.append("--test_filter="+test_filter)
    with (out/(name+".log")).open("w") as f:
        phase["exit"] = subprocess.run(cmd+owners, stdout=f, stderr=subprocess.STDOUT).returncode
    if not bep.is_file():
        return
    for line in bep.open():
        e = json.loads(line)
        if "testResult" not in e or "testResult" not in e["id"]:
            continue
        owner = e["id"]["testResult"]["label"]
        assert owner in owners
        t = e["testResult"]
        row = {"status": t.get("status"), "cached": bool(t.get("cachedLocally") or t.get("executionInfo",{}).get("cachedRemotely")),
               "cases": [], "wire": {}, "results": {}, "rto": {}}
        phase["owners"][owner] = row
        item = next(v for v in t.get("testActionOutput",[]) if v["name"]=="test.log")
        url = urlsplit(item["uri"])
        assert url.scheme == "file" and not url.netloc
        path = Path(unquote(url.path))
        assert path.name == "test.log"
        content = path.read_bytes()
        row["log_sha256"] = hashlib.sha256(content).hexdigest()
        for line in content.decode(errors="replace").splitlines():
            if m := re.search(r"--- (PASS|FAIL|SKIP): (Test(?:TCPUserTimeout|UserTimeoutOnNextFlight|ZeroWindowProbeUserTimeout|UserTimeoutBoundaryProbe)[a-zA-Z0-9/_]*) ", line):
                row["cases"].append(list(m.groups()))
            if m := re.search(r"BOUNDARY_WIRE mode=(fresh|partial|expires|eventual) letter=([wabcd]) offset=(\d+) us=(\d+)", line):
                row["wire"].setdefault(m[1],[]).append([m[2],int(m[3]),int(m[4])])
            if m := re.search(r"BOUNDARY_RTO mode=(fresh|partial|expires|eventual) rto_us=(\d+) retransmits=(\d+) backoff=(\d+)", line):
                row["rto"][m[1]] = list(map(int,m.groups()[1:]))
            if m := re.search(r"BOUNDARY_RESULT mode=(fresh|partial|expires|eventual) ((?:[a-z_]+=\d+ ?)+)", line):
                row["results"][m[1]]={k:int(v) for k,v in (field.split("=") for field in m[2].split())}
    phase["verified"] = phase["exit"] == 0 and set(phase["owners"])==set(owners) and all(
        row["status"]=="PASSED" and not row["cached"] and row["cases"] for row in phase["owners"].values())
    save()


root = "//test/packetimpact/tests:"
normal = [root+"tcp_user_timeout_"+arch+"_test" for arch in ["native","netstack"]]
persist = [root+"tcp_zero_window_probe_usertimeout_"+arch+"_test" for arch in ["native","netstack"]]
run("production", product, normal+persist)
run("packet", probe, normal, "^TestUserTimeoutBoundaryProbe$")
report["verified"] = all(p.get("verified") for p in report["phases"].values())
for row in report["phases"]["packet"]["owners"].values():
    expected = {"fresh": 0, "partial": 110, "expires": 0, "eventual": 110}
    report["verified"] &= {mode:v["errno"] for mode,v in row["results"].items()} == expected
    for mode, value in row["results"].items():
        report["verified"] &= row["rto"].get(mode, [0])[0] >= 2*value["rto_us"]

for label, row in report["phases"]["production"]["owners"].items():
    passed = {name for status,name in row["cases"] if status=="PASS"}
    required = {"TestUserTimeoutOnNextFlight/beforeNextRTO","TestUserTimeoutOnNextFlight/afterNextRTO"} if label in normal else {"TestZeroWindowProbeUserTimeout"}
    report["verified"] &= required <= passed
save()
raise SystemExit(not report["verified"])
