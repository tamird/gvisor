"""Read both packet cleanup TestRunner results from the original native Bazel invocation."""
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

base = "3847ed738e1a59892b936aebb1b9164c4f31ee95"
head = "4677b6d5ab8676013afe8e5074ca776e9aabaafd"
original = "1c156e11a9760fb68214c2483493f1d50d8836a9"
owners = {
    "//pkg/sentry/socket/netstack:netstack_test": ("TestDestroyWaitsForNICs", "TestRouteTableDumpOrder"),
    "//pkg/tcpip/stack:stack_x_test": ("TestRemoveNICWaitsForLinkResolution",
        "TestRemoveNICWaitsForLinkResolution/overflow=false", "TestRemoveNICWaitsForLinkResolution/overflow=true"),
}
area = Path(os.environ["RUNNER_TEMP"]) / "packet-cleanup"
report = {"source": head, "previous": original, "base": base, "owners": {}}


def save():
    (area / "result.json").write_text(json.dumps(report, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args])


def delta(*refs):
    return [line for line in git("diff", "--no-ext-diff", "-U0", *refs).splitlines() if
            line.startswith(b"diff --git ") or line[:1] in (b"+", b"-") and line[:3] not in (b"---", b"+++")]


def results(label, event):
    path = Path("bazel-testlogs", label[2:].replace(":", "/"))
    row = report["owners"][label] = {"status": event["status"], "strategy": event.get("executionInfo", {}).get("strategy"),
                                    "cached": event.get("cachedLocally", False)}
    files = {}
    for f in event.get("testActionOutput", []):
        name = f["name"]
        if name not in ("test.log", "test.xml"):
            continue
        uri = urlsplit(f["uri"])
        assert uri.scheme == "file" and not uri.netloc and Path(unquote(uri.path)).resolve() == (path / name).resolve()
        files[name] = (path / name).read_bytes()
        row[name] = hashlib.sha256(files[name]).hexdigest()
        assert not f.get("digest") or f["digest"] == row[name]
    counts, checked = {}, {}
    for case in ET.fromstring(files["test.xml"]).iter("testcase"):
        top, sep, sub = case.attrib["name"].partition("/")
        name = top.rsplit(".", 1)[-1] + sep + sub
        status = "SKIP" if case.find("skipped") is not None or case.get("status") in ("notrun", "suppressed") else (
            "FAIL" if case.find("error") is not None or case.find("failure") is not None else "PASS")
        counts[status] = counts.get(status, 0) + 1
        if name in owners[label]:
            assert name not in checked
            checked[name] = [status, ("--- PASS: " + name + " (").encode() in files["test.log"]]
    row.update(named=checked, counts=counts)
    row["passed"] = row["status"] == "PASSED" and row["strategy"] == "local" and not row["cached"] and all(
        checked.get(s) == ["PASS", True] for s in owners[label])


save()
assert platform.system() == "Linux" and platform.machine() == "x86_64"
assert git("rev-parse", "HEAD^").decode().strip() == head and git("rev-parse", head+"^").decode().strip() == base
assert git("rev-parse", head+"^{tree}").decode().strip() == "88012655ca010a342be0accef670ecf137f0e794"
assert git("show", "-s", "--format=%an <%ae>%n%at%n%B", head) == git("show", "-s", "--format=%an <%ae>%n%at%n%B", original)
assert delta(original+"^", original) == delta(base, head)
git("switch", "--quiet", "--detach", head)
assert not git("status", "--porcelain", "--untracked-files=no")
assert subprocess.check_output(["bazel", "--version"], text=True).strip() == "bazel 9.3.1"
report.update(exactDelta=True, machine=platform.machine())
save()
bep = area / "events.jsonl"
with (area / "console").open("w") as log:
    report["bazelExit"] = subprocess.run(["bazel", "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g",
        "test", "--config=x86_64", "--lockfile_mode=off", "--keep_going", "--jobs=4", "--remote_cache=", "--remote_executor=",
        "--bes_backend=", "--spawn_strategy=processwrapper-sandbox,local", "--strategy=TestRunner=local",
        "--test_sharding_strategy=disabled", "--nocache_test_results", "--flaky_test_attempts=1", "--test_timeout=600",
        "--test_env=GO_TEST_WRAP_TESTV=1", "--test_arg=-test.v", "--build_event_json_file="+str(bep), *owners],
        stdout=log, stderr=subprocess.STDOUT).returncode
save()
rows = {k: [] for k in owners}
if bep.is_file():
    for line in bep.open():
        event = json.loads(line)
        lbl = event.get("id", {}).get("testResult", {}).get("label")
        if lbl in rows and "testResult" in event:
            rows[lbl].append(event["testResult"])
for label, records in rows.items():
    report["owners"][label] = {"emitted": len(records)}
    if len(records) == 1:
        try:
            results(label, records[0])
        except (KeyError, AssertionError, OSError, ValueError, ET.ParseError) as e:
            report["owners"][label]["receiptError"] = type(e).__name__
    save()
if report["bazelExit"]:
    report["diagnostics"] = [s[:500] for s in (area / "console").read_text(errors="replace").splitlines() if s.startswith("ERROR:")][:6]
report["passed"] = report["bazelExit"] == 0 and all(r.get("passed") for r in report["owners"].values())
save()
raise SystemExit(not report["passed"])
