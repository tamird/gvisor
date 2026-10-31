"""Keep original Bazel exits and both architecture-specific verifier outputs."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys

root = Path(os.environ["RUNNER_TEMP"])
out = root / "proof"
out.mkdir(exist_ok=True)
stage = sys.argv[1]
def now():
    return datetime.now(timezone.utc).isoformat()

if stage == "collect":
    expected = ["normal", "unguarded"]
    arms = {k: json.loads((out / (k + ".json")).read_text())
            for k in expected if (out / (k + ".json")).exists()}
    report = {
        "arms": arms, "expected": expected, "missing": sorted(set(expected) - set(arms)),
        "failed": [k for k in arms if not arms[k]["verified"]], "verdictAt": now(),
    }
    (out / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    sys.exit(bool(report["failed"] or report["missing"]))

head = os.environ[{"normal": "SOURCE", "unguarded": "MISSING"}[stage]]
target = "//pkg/sentry/kernel:kernel_nogo"
docker = ["docker", "exec", "-e", "GIT_CONFIG_COUNT=1", "-e", "GIT_CONFIG_KEY_0=safe.directory",
          "-e", "GIT_CONFIG_VALUE_0=" + os.environ["GITHUB_WORKSPACE"], "overlay-guest"]
record = {"source": head, "target": target, "start": now(), "verified": False}
try:
    subprocess.run(["git", "checkout", "--quiet", "--detach", head], check=True)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == head
    assert not subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"])
    bep = root / (stage + ".bep")
    args = ["test", "--config=x86_64", "--jobs=5", "--nocache_test_results",
            "--strategy=TestRunner=remote", "--strategy=GoStaticAnalysis=linux-sandbox",
            "--strategy=GoStandardLibraryAnalysis=linux-sandbox", "--strategy=GoStdlib=linux-sandbox",
            "--strategy=GoStdlibList=linux-sandbox",
            "--modify_execution_info=^Go(StandardLibrary|Static)Analysis$=-no-sandbox,^Go(Stdlib(List)?|StandardLibraryAnalysis)$=+no-remote-cache",
            "--remote_download_outputs=toplevel", "--remote_download_regex=.*[/]test[.](log|xml)$",
            "--test_sharding_strategy=disabled", "--test_timeout=900", "--test_output=errors",
            "--build_event_json_file=" + str(bep), target]
    record["args"] = args
    log = root / (stage + ".bazel.log")
    with log.open("w") as f:
        proc = subprocess.Popen(docker + ["bazel", "--bazelrc=" + str(root / "overlay.bazelrc")] + args,
                                stdout=f, stderr=subprocess.STDOUT)
        while True:
            try:
                record["exit"] = proc.wait(timeout=55)
                break
            except subprocess.TimeoutExpired:
                print(stage, now(), log.stat().st_size, "log bytes", flush=True)
    record["completed"] = now()
    body = log.read_text(errors="replace")
    record["invocations"] = sorted(set(re.findall(r"https://app.buildbuddy.io/invocation/[a-f0-9-]+", body)))
    record["diagnostics"] = [line for line in body.splitlines() if re.match(r"^(ERROR:|FAIL:)", line)
                             and not re.search("header|secret|key=|token", line, re.I)][-15:]
    sums = []
    if bep.exists():
        for line in bep.open():
            event = json.loads(line)
            if event["id"].get("testSummary", {}).get("label") == target:
                sums.append(event["testSummary"]["overallStatus"])
    record["summaries"] = sums
    runlog = subprocess.run(docker + ["cat", "bazel-testlogs/pkg/sentry/kernel/kernel_nogo/test.log"],
                            capture_output=True, text=True)
    (out / (stage + ".test.log")).write_text(runlog.stdout)
    assert runlog.returncode == 0, runlog.stderr
    sections = re.split(r"(?m)^(amd64|arm64)\.\.\.", runlog.stdout)[1:]
    parts = dict(zip(sections[::2], sections[1::2]))
    assert len(sections) == 4 and set(parts) == {"amd64", "arm64"}, sections
    record["results"] = {}
    for arch, part in parts.items():
        lines = part.strip().splitlines()
        record["results"][arch] = lines
        if stage == "normal":
            assert lines == ["PASS"], lines
        else:
            assert len(lines) == 1 and re.search(
                r"checklocks: .*timekeeper\.go:392:\d+: must hold .*updateMu.* to call update, but not held", lines[0]
            ), lines
    expected = (0, ["PASSED"]) if stage == "normal" else (3, ["FAILED"])
    assert (record["exit"], sums) == expected, (record["exit"], sums)
    record["verified"] = True
except BaseException as err:
    record["error"] = repr(err)[:1400]
    raise
finally:
    record["verdictAt"] = now()
    (out / (stage + ".json")).write_text(json.dumps(record, indent=2) + "\n")
    print(stage, record["verified"], record.get("exit"), record.get("invocations"), record.get("error"), flush=True)
