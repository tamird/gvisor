"""Run the pinned agent's command phase against the original repository hooks."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys


W = Path(sys.argv[0]).resolve().parent
SRC = Path.cwd()
TARGET = "//test/rbe/coordinator_hook_fixture:sentinel"
CONFIGS = ("source-presubmit", "source-presubmit-arm64")
AGENT = str(W / "agent")
SIMPLE = "awk basename bash cat chmod cp cut date df dirname dmesg env find git grep head id ln ls mkdir mktemp mv pwd readlink realpath rm rmdir sed sh sort stat tail tar tee timeout touch tr uname uniq wc xargs".split()
FORBIDDEN = "sudo docker make apt apt-get apt-cache sysctl systemctl gcloud killall".split()
GROUP = sys.argv[1]


def is_isolated():
    status = Path("/proc/self/status").read_text()
    return (os.geteuid() != 0 and re.search(r"^NoNewPrivs:\s+1$", status, re.M) is not None
            and re.search(r"^CapEff:\s+0+$", status, re.M) is not None
            and re.search(r"^CapBnd:\s+0+$", status, re.M) is not None
            and not Path("/var/run/docker.sock").exists() and not Path("/run/docker.sock").exists()
            and bool(os.statvfs("/proc/sys").f_flag & os.ST_RDONLY)
            and {name for _, name in socket.if_nameindex()} == {"lo"})


def statuses(path):
    result = []
    if path.exists():
        for line in path.open():
            e = json.loads(line)
            if "testResult" in e:
                assert e["id"]["testResult"]["label"] == TARGET
                v = e["testResult"]
                result.append([v["status"], bool(v.get("cachedLocally") or v.get("executionInfo", {}).get("cachedRemotely"))])
    return result


def run_case(name, row):
    assert row["isolated"]
    legacy = name.startswith("legacy-")
    blocked = name == "stock-blocked"
    allowed = W / ("path-" + name)
    allowed.mkdir()
    for tool in SIMPLE:
        source = shutil.which(tool, path="/usr/local/bin:/usr/bin:/bin")
        if source:
            (allowed / tool).symlink_to(source)
    launcher = "bazel" if name in ("first-fails", "stock-blocked") else "bazelisk"
    for tool in [*FORBIDDEN, "buildkite-agent", launcher]:
        (allowed / tool).symlink_to(W / "dispatch.py")
    uid = "00000000-0000-4000-8000-000000000001"
    env = {"PATH": str(allowed), "HOME": os.environ["HOME"], "USER": os.environ["USER"], "LANG": "C.UTF-8",
           "HOOK_TASK": str(W), "HOOK_CASE": name, "BUILDKITE_AGENT_ENDPOINT": "http://127.0.0.1:1",
           "BUILDKITE_BUILD_CHECKOUT_PATH": str(SRC), "BUILDKITE_ORGANIZATION_SLUG": "offline",
           "BUILDKITE_PIPELINE_SLUG": "source", "BUILDKITE_PIPELINE_NAME": "Offline",
           "BUILDKITE_REPO": str(SRC), "BUILDKITE_AGENT_NAME": "offline-agent", "BUILDKITE_PIPELINE_PROVIDER": "github",
           "BUILDKITE_BRANCH": "test/coordinator-qualification", "BUILDKITE_JOB_ID": uid,
           "BUILDKITE_BUILD_ID": uid, "BUILDKITE_LABEL": "Offline hook fixture",
           "BUILDKITE_AGENT_META_DATA_QUEUE": "offline", "BUILDKITE_BIN_PATH": str(allowed)}
    env["BUILDKITE_COMMIT"] = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    render = subprocess.run([AGENT, "pipeline", "upload", "--dry-run", "--format", "json",
                             "--agent-access-token", "offline", ".buildkite/rbe-source.yaml"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    row["render_exit"] = render.returncode
    step, = json.loads(render.stdout)["steps"]
    assert step["label"] == "Remote source presubmit"
    env["BUILDKITE_COMMAND"] = "exit 19" if legacy else step["command"]
    env["BUILDKITE_STEP_KEY"] = "legacy-test" if legacy else step.get("key", "")
    done = Path("/tmp/last_job_completed_time")
    count = Path("/tmp/consecutive_failures")
    assigned = Path("/tmp/job_assigned")
    done.write_text("1\n")
    count.write_text("4\n")
    assigned.unlink(missing_ok=True)
    subprocess.run(["git", "tag", "-f", "buildkite-test-branch", "HEAD^"], check=True, stdout=subprocess.DEVNULL)
    with (W / (name + ".log")).open("wb") as output:
        p = subprocess.Popen([AGENT, "bootstrap", "--phases", "command", "--shell", "/bin/bash -e -c"],
                             env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            row["exit"] = p.wait(timeout=8 if blocked else 270)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
            row["deadline"] = True
    log = (W / (name + ".log")).read_text(errors="replace")
    trace = W / (name + ".jsonl")
    events = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
    row["source"] = env["BUILDKITE_COMMIT"]
    row["assigned"] = assigned.exists()
    row["completed"] = int(done.read_text()) > 1
    row["consecutive_failures"] = int(count.read_text()) if count.exists() else 0
    row["stamped"] = subprocess.check_output(["git", "rev-parse", "buildkite-test-branch"], text=True).strip() == row["source"]
    row["calls"] = [e for e in events if "forbidden" not in e][:32]
    row["forbidden"] = sorted({(e["forbidden"], e["operation"]) for e in events if "forbidden" in e})
    row["apt_retries"] = sum(e.get("operation") == "apt update" for e in events)
    row["log_sha256"] = hashlib.sha256(log.encode()).hexdigest()
    errors = []
    for line in re.sub(r"\x1b\[[0-9;]*m", "", log).splitlines():
        if not re.search(r"Missing |Error:|ERROR:|command not found|failed to|Permission denied|No such", line, re.I):
            continue
        if any(s in line.lower() for s in ("authorization:", "bearer ", "password=", "token=", "api-key=", "environment:")):
            continue
        s = re.sub(r"https?://\S+|/[A-Za-z0-9_./-]+", "<path>", line)
        s = re.sub(r"[A-Za-z0-9_-]{36,}", "<value>", s)
        if s[:300] not in errors:
            errors.append(s[:300])
    row["diagnostics"] = errors[:5]
    row["observations"] = [token for token in ("COORDINATOR_INTENTIONAL_TEST_FAILURE", "Bazel shutdown failed with status 27",
        "No such file", "command not found", "permission denied", "Permission denied", "must set either",
        "failed to verify", "not a git repository", "unknown flag", "Failed to start", "not found in") if token in log]
    assert row["assigned"] and any(e.get("offline_download") for e in events)
    if blocked:
        return (row.get("deadline") and ("docker", "ps") in row["forbidden"] and row["apt_retries"] >= 2
                and not any("test" in e for e in events) and not row["completed"])
    if legacy:
        return (row.get("exit") == 19 and row["stamped"] and row["completed"] and row["consecutive_failures"] == 5
                and [e["legacy_make"] for e in events if "legacy_make" in e] == ["-s testlogs", "clean", "bazel-shutdown"]
                and any(e.get("offline_legacy_log_upload") for e in events))
    expect = {"first-fails": CONFIGS[0], "second-fails": CONFIGS[1]}.get(name)
    tests = [e for e in events if "test" in e]
    row["results"] = {config: statuses(W / (name + "-" + config + ".bep")) for config in CONFIGS}
    return (row.get("exit") == (1 if expect else 0) and row["completed"] and row["stamped"]
            and row["consecutive_failures"] == (5 if expect else 0) and not row["forbidden"]
            and [e["test"] for e in tests] == list(CONFIGS)
            and all(e["reports_errors"] and e["stamp_policy"] and e["launcher"] == launcher for e in tests)
            and sum("shutdown" in e for e in events) == 1
            and [e["offline_failure_annotation"] for e in events if "offline_failure_annotation" in e] == ([[expect]] if expect else [])
            and ("COORDINATOR_INTENTIONAL_TEST_FAILURE" in log) == bool(expect)
            and ("Bazel shutdown failed with status 27" in log) == (name == "shutdown-fails")
            and row["results"] == {c: [["FAILED" if c == expect else "PASSED", False]] for c in CONFIGS})


cases = ["stock-blocked", "legacy-stock"] if GROUP == "stock" else ["both-pass", "first-fails", "second-fails", "shutdown-fails", "legacy-fixed"]
rows = {}
for case in cases:
    row = rows[case] = {"isolated": is_isolated(), "passed": False}
    try:
        row["passed"] = bool(run_case(case, row))
    except Exception as err:
        row["collector_error"] = type(err).__name__
    finally:
        (W / "results" / (GROUP + ".json")).write_text(json.dumps(rows, indent=2) + "\n")
sys.exit(0 if all(r["passed"] for r in rows.values()) else 1)
