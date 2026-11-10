"""Preserve both native Nogo test results and compare positions on identical input."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import unquote, urlsplit

ROOT = Path(os.environ["RUNNER_TEMP"]) / "semaphore-slice-guard"
RECEIPT = ROOT / "receipt"
TARGET = "//pkg/sentry/kernel/semaphore:semaphore_nogo"
SOURCE = Path("pkg/sentry/kernel/semaphore/semaphore.go")
REVISIONS = {
    "stock": "3cfe94a2fa36d31bd35d7f09fe2598227a5e4bca",
    "candidate": "3cef93ed944d840198deb620a2305f6ba74f32f4",
}
CONTROL = re.compile(r"// (valid|wrong owner|diagnostic|unguarded|lifetime|escape|unproven): (.*)")
report = {}


def save():
    (RECEIPT / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")


def collect(label, sha):
    subprocess.run(["git", "switch", "--quiet", "--detach", sha], check=True)
    cases = {i: list(match.groups()) for i, line in enumerate(SOURCE.read_text().splitlines(), 1)
             if (match := CONTROL.search(line))}
    assert len(cases) == 19, cases
    row = {"source": sha, "fileSha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(), "cases": cases}
    report[label] = row
    save()
    bep = ROOT / (label + ".bep")
    with (RECEIPT / (label + ".console")).open("w") as out:
        row["exit"] = subprocess.run([
            str(ROOT / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx2600m", "test",
            "--config=x86_64", "--keep_going", "--jobs=2", "--local_resources=memory=9900",
            "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
            "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox",
            "--spawn_strategy=linux-sandbox", "--strategy=TestRunner=local", "--remote_executor=", "--remote_cache=", "--bes_backend=",
            "--nocache_test_results", "--flaky_test_attempts=1", "--test_timeout=300", "--test_sharding_strategy=disabled",
            "--build_tag_filters=", "--test_tag_filters=", "--color=no", "--curses=no", "--test_output=errors",
            "--build_event_json_file=" + str(bep), TARGET
        ], stdout=out, stderr=subprocess.STDOUT).returncode
    save()
    results = []
    for line in bep.read_bytes().splitlines():
        event = json.loads(line)
        if "started" in event:
            row["invocation"] = event["started"]["uuid"]
        if event["id"].get("testResult", {}).get("label") == TARGET and "testResult" in event:
            results.append(event["testResult"])
    result, = results
    row["status"] = result["status"]
    info = result["executionInfo"]
    row["strategy"] = info.get("strategy")
    row["cached"] = bool(result.get("cachedLocally") or info.get("cachedRemotely"))
    for output in result["testActionOutput"]:
        if output["name"] not in ("test.log", "test.xml"):
            continue
        loc = urlsplit(output["uri"])
        assert loc.scheme == "file" and not loc.netloc, output["name"]
        shutil.copyfile(unquote(loc.path), RECEIPT / (label + "." + output["name"]))
    raw = (RECEIPT / (label + ".test.log")).read_bytes()
    row["testLogSHA256"] = hashlib.sha256(raw).hexdigest()
    sections = re.split(r"(?m)^(amd64|arm64)\.\.\.", raw.decode())
    row["architectures"] = {}
    for arch, section in zip(sections[1::2], sections[2::2]):
        diagnosed = {}
        unmapped = []
        for message in section.splitlines():
            if "checklocks:" not in message:
                continue
            match = re.search(r"semaphore\.go:(\d+):\d+: (.*)", message)
            if match:
                diagnosed.setdefault(int(match[1]), []).append(match[2])
            else:
                unmapped.append(message)
        negative = {"diagnostic"} if label == "stock" else {"diagnostic", "wrong owner", "unguarded", "escape", "unproven"}
        expected = {i for i, (kind, _) in cases.items() if kind in negative}
        row["architectures"][arch] = {
            "diagnosed": diagnosed, "unmapped": unmapped, "missing": sorted(expected - diagnosed.keys()),
            "additional": sorted(diagnosed.keys() - expected),
        }
    save()
    rows = row["architectures"]
    row["observed"] = (row["status"] == "FAILED" and row["exit"] == 3 and not row["cached"]
                       and row["strategy"] == "local" and set(rows) == {"amd64", "arm64"})
    row["matches"] = row["observed"] and all(not (r["missing"] or r["additional"] or r["unmapped"]) for r in rows.values())
    save()


for name, revision in REVISIONS.items():
    collect(name, revision)
assert report["stock"]["fileSha256"] == report["candidate"]["fileSha256"]
assert all(r["matches"] for r in report.values()), "See original results and classified controls in the receipt"
