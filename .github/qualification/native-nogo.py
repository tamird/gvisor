"""Exercise both Nogo job commands with an unrelated test and a real diagnostic."""

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "nogo-receipts"
EVENTS = Path(".build_events.json")
LABELS = {
    "//pkg/bits:bits_nogo",
    "//pkg/bits:bits_test_nogo",
    "//pkg/tcpip/seqnum:seqnum_nogo",
}
BAD = "//pkg/tcpip/seqnum:seqnum_nogo"
DIAGNOSTIC = "self-assignment of v to v"


def store(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2) + "\n")


def job(name):
    raw = Path(f".buildkite/{name}.yaml").read_text()
    label = '    label: ":golang: Nogo tests"\n'
    assert raw.count(label) == 1
    lines = raw.split(label)[1].splitlines()
    assert lines[0].startswith("    command: ")
    assert lines[1:3] == ["    env:", '      DOCKER_BUILD: "false"']
    command = shlex.split(lines[0].removeprefix("    command: "))
    assert command[0:2] == ["bazel", "test"] and command[-1] == "//..."
    return command


def run(name, command, deadline):
    # Only narrow the target patterns. Machine resource controls are startup
    # config, not replacement test/selection/reporting options.
    cmd = command[:1] + [f"--bazelrc={TMP}/nogo-resources.bazelrc"] + command[1:-1]
    cmd += ["//pkg/bits/...", "//pkg/tcpip/seqnum/..."]
    EVENTS.unlink(missing_ok=True)
    log = TMP / f"{name}.out"
    with log.open("w") as out:
        code = subprocess.run(["timeout", "--kill-after=10s", deadline, *cmd], stdout=out, stderr=out).returncode
    console = log.read_text(errors="replace")
    # Never publish Bazel command lines, headers or the raw BEP.
    errors = [line[:420] for line in console.splitlines() if line.startswith("ERROR:") and "header" not in line.lower()][:5]
    expected = {label: ("FAILED" if name == "bad" and label == BAD else "PASSED") for label in LABELS}
    summaries = {}
    executions = {}
    markers = {}
    if EVENTS.exists():
        for line in EVENTS.read_text().splitlines():
            event = json.loads(line)
            if "testSummary" in event:
                label = event["id"]["testSummary"]["label"]
                summaries[label] = event["testSummary"]["overallStatus"]
            if "testResult" in event:
                label = event["id"]["testResult"]["label"]
                result = event["testResult"]
                executions[label] = result["status"]
                if label in LABELS:
                    path = Path("bazel-testlogs") / label[2:].replace(":", "/") / "test.log"
                    if path.is_file():
                        content = path.read_text(errors="replace")
                        markers[label] = {arch: f"{arch}..." in content for arch in ("amd64", "arm64")}
                        if name == "bad" and label == BAD:
                            (OUT / "failed-test.log").write_text(content)
    report = {"exit": code, "expected": expected, "summaries": summaries, "executions": executions, "architectures": markers, "errors": errors}
    store(f"{name}.json", report)
    assert (code, summaries, executions) == (3 if name == "bad" else 0, expected, expected), report
    assert set(markers) == LABELS and all(all(m.values()) for m in markers.values()), markers
    return report


def log_hook():
    # Exercise the actual post-command upload loop; replace only its remote
    # Buildkite destination with a recorder.
    scripts = Path(".buildkite/hooks/post-command").read_text()
    block = scripts.split("# Upload all relevant test failures.\n")[1].split("# Upload all profiles,")[0]
    fake = TMP / "nogo-mock"
    fake.mkdir()
    agent = fake / "buildkite-agent"
    agent.write_text("""#!/bin/bash
set -eu
case "$1 $2" in
  'artifact upload') test -r "$3"; printf '%s\\n' "$3" >> "$RUNNER_TEMP/nogo-mock/uploads";;
  'annotate --style') cat >> "$RUNNER_TEMP/nogo-mock/annotations";;
  *) exit 2;;
esac
""")
    agent.chmod(0o755)
    env = dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}")
    subprocess.run(["bash", "-e", "-c", block], env=env, check=True)
    uploads = (fake / "uploads").read_text().splitlines()
    assert len(uploads) == 1, uploads
    log = Path(uploads[0])
    assert str(log).endswith("/pkg/tcpip/seqnum/seqnum_nogo/test.log"), log
    assert DIAGNOSTIC in log.read_text(), log
    annotation = (fake / "annotations").read_text()
    assert f"[{BAD}](artifact://{str(log).lstrip('/')})" in annotation, annotation
    store("post-command.json", {"target": BAD, "artifact": str(log), "annotation": annotation.strip(), "diagnostic": DIAGNOSTIC})


def main():
    first, second = job("first-pass"), job("pipeline")
    assert first == second
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == os.environ["PRODUCT"]
    run("clean", first, "40m")
    assert not subprocess.check_output(["make", "-s", "testlogs"], text=True).strip()
    source = Path("pkg/tcpip/seqnum/seqnum.go")
    text = source.read_text()
    anchor = "func (v Value) LessThan(w Value) bool {\n"
    assert text.count(anchor) == 1
    source.write_text(text.replace(anchor, anchor + "\tv = v\n"))
    run("bad", second, "8m")
    log_hook()
    store("verdict.json", {"verified": True, "source": os.environ["PRODUCT"], "buildkite_command": first, "label_count": len(LABELS), "plain_go_test_excluded": "//pkg/bits:bits_test", "builder": False})


if __name__ == "__main__":
    main()
