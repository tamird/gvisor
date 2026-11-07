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


def startup_summary(log):
    # A bootstrap with no external job token or environment from the caller.
    # Keep only the early error (never an environment dump, command or header).
    plain = re.sub(r"\x1b\[[0-9;]*m", "", log)
    lines = []
    for line in plain.splitlines():
        if not re.search(r"error|fail|required|invalid|missing|cannot|must|unable|fatal|expected|flag|unknown|not found|no such|not set", line, re.I):
            continue
        if any(s in line.lower() for s in ("authorization:", "bearer ", "password=", "token=", "api-key=")):
            continue
        line = re.sub(r"https?://\S+|/[A-Za-z0-9_./-]+", "<path>", line)
        line = re.sub(r"[A-Za-z0-9_-]{36,}", "<value>", line)
        lines.append(line[:350])
    if not lines and len(plain) < 512 and "=" not in plain and "://" not in plain:
        lines = [re.sub(r"/[A-Za-z0-9_./-]+", "<path>", s)[:180] for s in plain.splitlines()[:2]]
    return lines[:3]


def bootstrap(env, path, timeout):
    with path.open("wb") as output:
        p = subprocess.Popen([AGENT, "bootstrap", "--phases", "command", "--shell", "/bin/bash -e -c"],
                             env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
            return None


def corrected_startup(env, case, row):
    choice = W / "chosen-startup.json"
    if choice.exists():
        return json.loads(choice.read_text())["settings"]
    if GROUP != "stock" or case != "stock-blocked":
        raise RuntimeError("original startup has not reached its hook")
    opts = [{}]
    repairs = [
        (r"job api|socket", {"BUILDKITE_SOCKETS_PATH": "/tmp/agent-sockets"}),
        (r"build.path|checkout.path", {"BUILDKITE_BUILD_PATH": "/tmp/agent-builds"}),
        (r"hooks.path|global hook", {"BUILDKITE_HOOKS_PATH": "/tmp/agent-global-hooks"}),
        (r"repository|BUILDKITE_REPO", {"BUILDKITE_REPO": str(SRC)}),
        (r"agent.name", {"BUILDKITE_AGENT_NAME": "offline-agent"}),
        (r"pty|terminal", {"BUILDKITE_PTY": "false"}),
        (r"shell|interpreter", {"BUILDKITE_HOOKS_SHELL": "/bin/bash"}),
        (r"access.token", {"BUILDKITE_AGENT_ACCESS_TOKEN": "offline"}),
        (r"bin.path", {"BUILDKITE_BIN_PATH": ""}),
    ]
    Path("/tmp/agent-global-hooks").mkdir(exist_ok=True)
    Path("/tmp/agent-builds").mkdir(exist_ok=True)
    original_hash = "51ad3d2a9fdff0cfa2da4294878c16a9b7898a64ef8de7f2f476e7f51d86e1b1"
    adjustments = {}
    row["startup_probes"] = probes = []
    for extra in opts:
        trace = W / (case + ".jsonl")
        trace.unlink(missing_ok=True)
        log = W / "startup-probe.log"
        rc = bootstrap({**env, **extra}, log, 4)
        data = log.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        probes.append({"selectors": sorted(extra), "original_hash": digest == original_hash,
                       "exit": rc, "diagnostic": startup_summary(data.decode(errors="replace"))})
        if not extra:
            assert digest == original_hash
            for pattern, settings in repairs:
                if re.search(pattern, data.decode(errors="replace"), re.I):
                    opts.append(settings)
        if trace.exists() and '"apt update"' in trace.read_text():
            adjustments = extra
            break
    else:
        raise RuntimeError("no source hook exercised")
    assert adjustments
    choice.write_text(json.dumps({"settings": adjustments}))
    (W / (case + ".jsonl")).unlink(missing_ok=True)
    return adjustments


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
    extra = corrected_startup(env, name, row)
    env.update(extra)
    row["startup_selectors"] = sorted(extra)
    done = Path("/tmp/last_job_completed_time")
    count = Path("/tmp/consecutive_failures")
    assigned = Path("/tmp/job_assigned")
    done.write_text("1\n")
    count.write_text("4\n")
    assigned.unlink(missing_ok=True)
    subprocess.run(["git", "tag", "-f", "buildkite-test-branch", "HEAD^"], check=True, stdout=subprocess.DEVNULL)
    rc = bootstrap(env, W / (name + ".log"), 8 if blocked else 270)
    if rc is None:
        row["deadline"] = True
    else:
        row["exit"] = rc
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
    if not row["assigned"]:
        row["startup_diagnostic"] = startup_summary(log)
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
