"""Read original Bazel attempts and the existing packet-ring syscall case."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(os.environ["RUNNER_TEMP"])
NATIVE = "//test/syscalls:packet_mmap_test_native"
GUEST = "//test/syscalls:packet_mmap_test_runsc_systrap_directfs"
CASE = "PacketMmapTest.PacketReserve"
TEST = "test/syscalls/linux/packet_mmap.cc"
SOURCE = ["pkg/sentry/socket/netstack/netstack.go", "pkg/tcpip/tcpip.go",
          "pkg/tcpip/transport/packet/endpoint.go"]
FIX = "5de0b08c436460cd3f68cf9a3dfc0bef8f891407"
BASE = "f51a205a8f4afbc5ab41aded070df74951b081ea"
report = {"fixed": FIX, "base": BASE, "case": CASE, "attempts": {}}


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def save():
    (ROOT / "packet-reserve.json").write_text(json.dumps(report, indent=2) + "\n")


def run(arm, labels):
    bep = ROOT / (arm + ".bep")
    args = ["bazelisk", "--nosystem_rc", "--nohome_rc", "--bazelrc=" + str(ROOT / "packet.rc"),
            "test", "--config=x86_64", "--nostamp", "--keep_going", "--jobs=48",
            "--inject_repository=packet_worker=" + str(ROOT / "packet-worker"),
            "--extra_execution_platforms=@packet_worker//:gnu", "--host_platform=@packet_worker//:gnu",
            "--spawn_strategy=remote", "--strategy=TestRunner=remote", "--noremote_local_fallback",
            "--remote_download_outputs=toplevel", "--remote_download_regex=(test[.]log|test[.]xml)$",
            "--bes_backend=", "--test_sharding_strategy=disabled",
            "--nocache_test_results", "--flaky_test_attempts=1", "--test_timeout=300",
            """--run_under=/usr/bin/unshare --user --map-root-user --net /bin/sh -ec '/sbin/ip link set lo up; exec "$@"' packet-test""",
            "--test_filter=" + CASE, "--build_event_json_file=" + str(bep), *labels]
    with (ROOT / (arm + ".console")).open("wb") as out:
        code = subprocess.run(args, stdout=out, stderr=subprocess.STDOUT).returncode
    events = [json.loads(line) for line in bep.read_text().splitlines()] if bep.is_file() else []
    row = report["attempts"][arm] = {"exit": code, "targets": {}}
    console = (ROOT / (arm + ".console")).read_bytes().lower()
    row["diagnostics"] = [s for s in ("no such target", "absolute path inclusion", "no matching platform",
                                      "permission denied", "operation not permitted", "unrecognized option",
                                      "no such file or directory", "action failed", "failed to fetch",
                                      "unable to find", "no space left", "timed out") if s.encode() in console]
    row["invocation"] = next((e["started"]["uuid"] for e in events if "started" in e), None)
    for label in labels:
        target = row["targets"][label] = {"verified": False}
        results = [e["testResult"] for e in events if e.get("id", {}).get("testResult", {}).get("label") == label]
        if len(results) != 1:
            continue
        result, = results
        info = result.get("executionInfo", {})
        target.update(status=result["status"], strategy=info.get("strategy"),
                      cached=bool(result.get("cachedLocally") or info.get("cachedRemotely")))
        path = Path("bazel-testlogs/test/syscalls") / label.split(":")[1] / "test.log"
        if not path.is_file():
            continue
        content = path.read_bytes()
        text = content.decode(errors="replace")
        target["log_sha256"] = hashlib.sha256(content).hexdigest()
        target["pass"] = bool(re.search(r"\[\s*OK\s*\]\s+" + re.escape(CASE) + r"\b", text))
        target["fail"] = bool(re.search(r"\[\s*FAILED\s*\]\s+" + re.escape(CASE) + r"\b", text))
        target["getsockopt"] = "getsockopt(" in text
        target["enoprotoopt"] = bool(re.search(r"Protocol not available|ENOPROTOOPT|errno[:= ]+92", text, re.I))
        target["verified"] = (not target["cached"] and target["strategy"] == "remote"
                              and ((result["status"] == "PASSED" and target["pass"] and not target["fail"])
                                   if arm == "fixed" else
                                   (result["status"] == "FAILED" and target["fail"] and target["getsockopt"]
                                    and target["enoprotoopt"])))
    row["verified"] = code == (0 if arm == "fixed" else 3) and all(t["verified"] for t in row["targets"].values())
    save()


save()
assert git("rev-parse", "HEAD") == FIX
report["test_blob"] = git("rev-parse", FIX + ":" + TEST)
run("fixed", [NATIVE, GUEST])
git("restore", "--source=" + BASE, "--", *SOURCE)
assert git("diff", "--name-only", BASE, "--", "pkg", "test").splitlines() == [TEST]
assert git("hash-object", TEST) == report["test_blob"]
run("stock", [GUEST])
report["verified"] = all(a["verified"] for a in report["attempts"].values())
save()
if not report["verified"]:
    raise SystemExit(1)
