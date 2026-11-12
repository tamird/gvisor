"""Qualify exact ARM64 sources and an isolated synthetic socketpair control."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

root = Path(os.environ["RUNNER_TEMP"]) / "trace-retries"
results = root / "results"
testfile = Path("test/trace/trace_test.go")
remote = Path("pkg/sentry/seccheck/sinks/remote")
candidate = subprocess.check_output(["git", "rev-parse", "HEAD^"], text=True).strip()
base = subprocess.check_output(["git", "rev-parse", "HEAD^^"], text=True).strip()
assert base == "56ffe22724925a43ab36b6f1a72009e0a69e7c81"
summary = {"base": base, "candidate": candidate, "testResults": []}

problems = []

def save():
    (results / "report.json").write_text(json.dumps(summary, indent=2) + "\n")

def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()

def qualify(label, target, test, reps, retries):
    bep = root / (label + ".bep")
    args = [str(root / "bazelisk"), "--nosystem_rc", "--nohome_rc", "--bazelrc=" + str(root / "cache.bazelrc"), "test",
            "--config=aarch64", "--lockfile_mode=off", "--jobs=4", "--local_resources=memory=9216",
            "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
            "--strategy=TestRunner=local", "--local_test_jobs=1", "--nocache_test_results", "--flaky_test_attempts=1",
            "--runs_per_test=" + str(reps), "--test_filter=^" + test + "$", "--test_arg=-test.v",
            "--test_timeout=180", "--test_output=all", "--color=no", "--curses=no", "--noshow_progress",
            "--nobuild_event_json_file_path_conversion",
            "--build_event_json_file=" + str(bep)]
    if retries is not None:
        args.append("--test_env=QUALIFICATION_TRACE_RETRIES=" + str(retries))
    args.append(target)
    print(label, target, test, reps, flush=True)
    console = results / (label + ".console")
    with console.open("wb") as out:
        status = subprocess.run(args, stdout=out, stderr=subprocess.STDOUT).returncode
    for line in console.read_text(errors="replace").splitlines():
        if ("--- PASS:" in line or "messages for MESSAGE_SYSCALL_FCNTL" in line or
                "consumer resumed on first send result" in line or "synthetic marker" in line):
            print(label, line, flush=True)
    raw = results / (label + "-bazel-testlogs")
    target_logs = Path("bazel-testlogs") / target.removeprefix("//").replace(":", "/")
    for path in target_logs.rglob("test.*"):
        if path.name in ("test.log", "test.xml") and path.is_file():
            dest = raw / path.relative_to(target_logs)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
    rows = []
    for line in bep.read_text().splitlines():
        item = json.loads(line)
        identity = item["id"].get("testResult", {})
        if identity.get("label") != target:
            continue
        result = item["testResult"]
        dest = results / (label + "-" + str(identity.get("run", 1)))
        dest.mkdir()
        for output in result.get("testActionOutput", []):
            if output["name"] in ("test.xml", "test.log"):
                path = Path(unquote(urlsplit(output["uri"]).path))
                shutil.copy(path, dest / output["name"])
        row = {"label": label, "source": git("rev-parse", "HEAD"), "testBlob": git("hash-object", str(testfile)),
               "target": target, "run": identity.get("run", 1), "status": result.get("status"),
               "cached": bool(result.get("cachedLocally") or result.get("executionInfo", {}).get("cachedRemotely")),
               "strategy": result.get("executionInfo", {}).get("strategy")}
        summary["testResults"].append(row)
        save()
        content = (dest / "test.log").read_text(errors="replace")
        cases = [c for c in ET.parse(dest / "test.xml").iter("testcase") if c.get("name") == test]
        row["tests"] = [c.get("name") for c in cases]
        row["passInLog"] = "--- PASS: " + test + " (" in content
        if retries is not None:
            expected = "synthetic marker absent, dropped=1" if retries == 0 else "synthetic marker received intact, dropped=0"
            row["marker"] = expected in content
            row["congestionObserved"] = "consumer resumed on first send result:" in content
        else:
            row["fcntlEvents"] = re.findall(r"Processed (\d+) messages for MESSAGE_SYSCALL_FCNTL", content)
            row["eligibleEagainDrops"] = content.count("Write failed, dropping point: resource temporarily unavailable")
        rows.append(row)
        save()
    print(label, status, rows, flush=True)
    if status or len(rows) != reps or not all(row["status"] == "PASSED" and not row["cached"] and
                row["strategy"] == "local" and row["tests"] == [test] and row["passInLog"] and
                (row["marker"] and row["congestionObserved"] if retries is not None else
                 len(row["fcntlEvents"]) == 1 and int(row["fcntlEvents"][0]) > 0) for row in rows):
        problems.append(label)

for label, revision in (("stock", base), ("candidate", candidate)):
    git("checkout", "--detach", revision)
    assert not git("status", "--porcelain", "--untracked-files=no")
    text = testfile.read_text()
    count = re.search(r'"retries":\s+(\d+),', text)
    retries = int(count[1]) if count else 0
    assert retries == (0 if label == "stock" else 10)
    try:
        qualify(label + "-normal", "//test/trace:trace_test", "TestAll", 2, None)
    except (OSError, RuntimeError, ValueError, ET.ParseError) as err:
        problems.append(label + "-normal: " + str(err))
    build = remote / "BUILD"
    fixture = remote / "trace_sink_fixture_test.go"
    config = build.read_text()
    key = '    srcs = ["remote_test.go"],'
    start = config.index('go_test(')
    assert config.count(key) == 1
    edited = config[start:].replace(key, '    srcs = ["remote_test.go", "trace_sink_fixture_test.go"],').replace('        "//pkg/fd",','        "//pkg/fd",\n        "//pkg/log",')
    assert '"//pkg/log"' in edited
    build.write_text(config[:start] + edited)
    shutil.copy(root / "trace_sink_fixture.go", fixture)
    try:
        qualify(label + "-socketpair", "//pkg/sentry/seccheck/sinks/remote:remote_test", "TestIsolatedTraceSinkRetry", 2, retries)
    except (OSError, RuntimeError, ValueError, ET.ParseError) as err:
        problems.append(label + "-socketpair: " + str(err))
    finally:
        build.write_text(config)
        fixture.unlink()
    assert not git("status", "--porcelain", "--untracked-files=no")

summary["problems"] = problems
save()
if problems:
    raise RuntimeError(str(problems))
