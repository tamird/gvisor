"""Measure two exact FUSE revisions using identical code and Bazel's original owner."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import unquote, urlsplit

BASE = "70008149cff7b8d3b858877ae9c359b9046d19c5"
PRODUCT = "cf38192044626b448d1bac5ceaab8f1991ccbace"
WORK = Path(os.environ["RUNNER_TEMP"]) / "fuse-storage"
RESULTS = WORK / "results"
OWNER = "//pkg/sentry/fsimpl/fuse:fuse_test"
PACKAGE = Path("pkg/sentry/fsimpl/fuse")
BUILD = PACKAGE / "BUILD"
FIXTURE = PACKAGE / "descriptor_storage_test.go"


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def run(revision, stage, targets, flags):
    log = WORK / (stage + ".log")
    bep = WORK / (stage + ".bep")
    command = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "test",
               "--config=x86_64", "--jobs=3", "--local_resources=memory=HOST_RAM*.65",
               "--build_event_json_file=" + str(bep), "--nocache_test_results", "--test_output=errors", "--color=no", "--curses=no",
               *flags, *targets]
    with log.open("wb") as stream:
        proc = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT)
    raw = log.read_text(errors="replace")
    status = {"source": revision, "exit": proc.returncode, "targets": targets,
              "diagnostics": [s for s in ("no matching toolchains", "no such target", "no such package",
                          "build interrupted", "timed out", "out of memory", "compilepkg:", "panic:",
                          "missing strict dependencies", "syntax error", "unknown flag", "no space left")
                          if s in raw.lower()]}
    if proc.returncode:
        status["locations"] = sorted(set(re.findall(r"(?:^|/)([\w.]+\.(?:go|bzl|cc):[0-9]+)", raw)))[:20]
    records = {}
    owner_log = ""
    if bep.is_file():
        for line in bep.open():
            event = json.loads(line)
            if "testResult" not in event:
                continue
            label = event["id"]["testResult"]["label"]
            assert label in targets and label not in records
            result = event["testResult"]
            info = result.get("executionInfo", {})
            records[label] = {"status":result["status"], "cached":bool(result.get("cachedLocally") or info.get("cachedRemotely"))}
            if label == OWNER:
                item = next((v for v in result.get("testActionOutput",[]) if v["name"]=="test.log"), {})
                uri = urlsplit(item.get("uri",""))
                if not item or (uri.scheme != "file" and not item.get("digest")):
                    continue
                path = Path(unquote(uri.path)) if uri.scheme == "file" else Path("bazel-testlogs/pkg/sentry/fsimpl/fuse/fuse_test/test.log")
                if path.is_file():
                    data = path.read_bytes()
                    digest = hashlib.sha256(data).hexdigest()
                    assert not item.get("digest") or item["digest"] == digest
                    owner_log = data.decode(errors="replace")
                    records[label]["log_sha256"] = digest
    status["test_results"] = records
    status["owners_passed"] = set(records)==set(targets) and all(v["status"]=="PASSED" and not v["cached"] for v in records.values())
    return status, owner_log


def main():
    assert git("rev-parse", PRODUCT+"^")==BASE
    assert sorted(git("diff", "--name-only", BASE, PRODUCT).splitlines()) == [str(BUILD),str(PACKAGE/"regular_file.go")]
    reports = {}
    for rev in (BASE, PRODUCT):
        git("checkout", "--quiet", "--detach", rev)
        assert git("rev-parse","HEAD")==rev
        # Only the measuring fixture and its BUILD list are added to either source.
        src = BUILD.read_text()
        anchor='        "connection_test.go",\n'
        assert src.count(anchor)==1 and not FIXTURE.exists()
        BUILD.write_text(src.replace(anchor,anchor+'        "descriptor_storage_test.go",\n'))
        shutil.copyfile(WORK/"measure.go",FIXTURE)
        after,text = run(rev,rev[:9],[OWNER],["--test_filter=^TestFUSEDescriptorCheckpoint$","--test_arg=-test.v",
                         "--test_arg=-test.count=8","--test_arg=-test.bench=^BenchmarkFUSERegularFile$",
                         "--test_arg=-test.benchtime=10000x"])
        rows=re.findall(r'(?m)^BenchmarkFUSERegularFile(?:-\d+)?\s+10000\s+[\d.]+ ns/op\s+\d+ B/op\s+\d+ allocs/op\s*$',text)
        facts=re.findall(r"FUSE-STORAGE codec=(graph|wire) struct=(\d+) checkpoint=(\d+) offset=(\d+) handle=(\d+)",text)
        after.update(benchmarks=len(rows),codec_counts={c:sum(f[0]==c for f in facts) for c in ("graph","wire")},
                     observed=sorted(set(facts)),case_failures=re.findall(r"(?m)^--- FAIL: (Test[\w/]+)",text))
        after["verified"]=after["exit"]==0 and after["owners_passed"] and len(rows)==8 and len(set(facts))==2 and all(v==8 for v in after["codec_counts"].values()) and all(f[3:]==("12345","91") for f in facts)
        (RESULTS/rev[:9]).write_text("\n".join(r.strip() for r in rows)+"\n")
        reports[rev[:9]]=after
        FIXTURE.unlink()
        git("restore","--",str(BUILD))
        assert not git("diff","--name-only","HEAD")
    after,text=run(PRODUCT,"owners",[OWNER,"//pkg/sentry/fsimpl/fuse:fuse_nogo"],["--test_arg=-test.v"])
    after.update(passed=re.findall(r"(?m)^--- PASS: (Test\w+)",text),skipped=re.findall(r"(?m)^--- SKIP: (Test\w+)",text))
    after["verified"]=after["exit"]==0 and after["owners_passed"] and "TestFUSEReadError" in after["passed"]
    reports["owners"]=after
    (RESULTS/"receipt.json").write_text(json.dumps(reports,indent=2)+"\n")
    if not all(r["verified"] for r in reports.values()):
        raise SystemExit("incomplete fixture or package results; see receipt")


if __name__=="__main__":
    main()
