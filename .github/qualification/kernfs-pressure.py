"""Compare a watched live /proc/self/fdinfo entry before and after LRU churn."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

from qualification_report import xml_cases

TEMP = Path(os.environ["RUNNER_TEMP"])
OUT = TEMP / "records"
SOURCE = os.environ["SOURCE"]
CASE = "Inotify.KernfsDynamicFileUnderCachePressure"
PROPOSAL = "60fd20b126b613f4743e8505b194f8a183b068f9"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save(name: str, data: object) -> None:
    (OUT / (name + ".json")).write_text(json.dumps(data, indent=2) + "\n")


def run(platform: str) -> None:
    assert platform == "runsc_ptrace", platform
    actual_source = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    assert actual_source == SOURCE, (actual_source, SOURCE)
    target = "//test/syscalls:inotify_test_" + platform
    key = str(uuid.uuid4())
    runner = "remote"
    record = dict(source=SOURCE, platform=platform, executor=runner, target=target,
                  invocation=key, start=now(), verified=False,
                  fixture_sha256=hashlib.sha256(Path("test/syscalls/linux/inotify.cc").read_bytes()).hexdigest())
    save(platform, record)
    bep = TEMP / (platform + ".bep")
    docker = ["docker", "exec", "-e", "GIT_CONFIG_COUNT=1", "-e", "GIT_CONFIG_KEY_0=safe.directory",
              "-e", "GIT_CONFIG_VALUE_0=" + os.environ["GITHUB_WORKSPACE"], "overlay-guest"]
    args = [*docker, "timeout", "39m", "bazel", "--bazelrc=" + str(TEMP / "overlay.bazelrc"),
            "test", "--config=x86_64", "--jobs=40", "--invocation_id=" + key,
            "--build_event_json_file=" + str(bep), "--remote_download_outputs=toplevel",
            "--remote_download_regex=.*[/]test[.](log|xml)$", "--nocache_test_results",
            "--modify_execution_info=^TestRunner$=+no-remote-cache", "--test_sharding_strategy=disabled",
            "--test_env=GTEST_FILTER=" + CASE, "--test_timeout=300",
            "--strategy=TestRunner=" + runner, "--test_output=all"]
    args.append("--test_arg=--use-tmpfs=true")
    log = TEMP / (platform + ".console")
    with log.open("wb") as stream:
        rc = subprocess.run([*args, target], stdout=stream, stderr=subprocess.STDOUT).returncode
    record.update(exit=rc, finished=now())
    observed = []
    if bep.exists():
        with bep.open() as events:
            for line in events:
                if not line.endswith("\n"):
                    continue
                obj = json.loads(line)
                if obj.get("id", {}).get("testResult", {}).get("label") != target:
                    continue
                result = obj["testResult"]
                info = result.get("executionInfo", {})
                observed.append(dict(status=result.get("status"), strategy=info.get("strategy"),
                                     cached=bool(result.get("cachedLocally") or info.get("cachedRemotely"))))
    record["results"] = observed
    diagnostics = [line[:480] for line in log.read_text(errors="replace").splitlines()
                   if re.search(r"FAILED:|ERROR:|error:", line, re.I)
                   and not re.search(r"token|[Hh]eader|secret|[Kk]ey=|https?://|grpcs?://", line)]
    record["diagnostics"] = diagnostics[-16:]
    if rc in (0, 3) and len(observed) == 1:
        relative = "bazel-testlogs/test/syscalls/" + target.partition(":")[2] + "/test.xml"
        xml = subprocess.run([*docker, "cat", relative], capture_output=True)
        if xml.returncode == 0 and xml.stdout:
            path = OUT / (platform + ".xml")
            path.write_bytes(xml.stdout)
            counts = xml_cases(path)
            cases = {node.get("classname", "") + "." + node.get("name", ""):
                     [(f.text or f.get("message", "")) for f in node if f.tag in ("failure", "error")]
                     for node in ET.parse(path).iter("testcase")}
            record.update(counts=counts, cases=cases)
            errors = cases.get(CASE, [])
            common = (set(cases) == {CASE} and len(list(ET.parse(path).iter("testcase"))) == 1
                      and counts["skipped"] == 0 and not observed[0]["cached"] and observed[0]["strategy"] == runner)
            reproduced = (rc == 3 and observed[0]["status"] == "FAILED" and counts["failed"] == 1
                          and len(errors) == 2 and all("after 1024 distinct procfs lookups" in e for e in errors)
                          and any("got 0 event" in e for e in errors)
                          and any("has a value 2" in e for e in errors))
            passed = rc == 0 and observed[0]["status"] == "PASSED" and counts["passed"] == 1 and not errors
            record["outcome"] = "pressure_failure" if reproduced else "passed" if passed else "incomplete"
            record["verified"] = bool(common and (reproduced or passed))
    save(platform, record)
    print(platform, rc, record.get("outcome"), record["verified"])


def verdict() -> bool:
    path = OUT / "runsc_ptrace.json"
    record = json.loads(path.read_text()) if path.exists() else {}
    ok = record.get("verified") is True and record["source"] == SOURCE
    save("verdict", dict(verified=ok, source=SOURCE, proposal=PROPOSAL, case=CASE,
                         original_control_artifact=13129436691,
                         outcome=record.get("outcome") if ok else "incomplete", verified_at=now()))
    return ok


if __name__ == "__main__":
    if sys.argv[1] == "verdict":
        sys.exit(not verdict())
    run(sys.argv[1])
