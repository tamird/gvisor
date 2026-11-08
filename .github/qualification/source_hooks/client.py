"""Observe the production hooks with Buildkite's offline command bootstrap."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys

WORK = Path(__file__).resolve().parent
FIXED, BASE = (WORK / "sources").read_text().splitlines()
BINARY = {"matching": "bazel-9.3.1", "wrong": "bazel-8.5.0"}
CASES = {
    "stock_missing": ("stock", False, None, True, 127),
    "stock_wrong": ("stock", False, "wrong", True, 73),
    "installed_bazelisk": ("fixed", True, "wrong", False, 0),
    "plain_without_go": ("fixed", False, "matching", False, 0),
    "go_only": ("fixed", False, None, True, 0),
    "wrong_with_go": ("fixed", False, "wrong", True, 0),
    "wrong_without_go": ("fixed", False, "wrong", False, 1),
    "governance_bazel_name": ("fixed", True, None, False, 0),
}
UTILS = ("awk basename bash cat chmod cp cut date df dirname dmesg env find git grep head "
         "id ln ls mkdir mktemp mv ps pwd readlink realpath rm rmdir sed sh sort stat tail "
         "tar tee timeout touch tr uname uniq wc xargs").split()
DENIED = "sudo docker make apt apt-get apt-cache sysctl systemctl gcloud killall".split()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture(args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def safe_line(log):
    phrases = ("Error:", "ERROR:", "Traceback", "No such file", "command not found",
               "must set", "denied", "failed", "not found", "requires go")
    return [re.sub(r"/[\w./-]+", "<path>", line)[:240] for line in log.splitlines()
            if any(part in line for part in phrases) and "token" not in line.lower()][:8]


def payload():
    name = os.environ["HOOK_CASE"]
    client = Path(os.environ["BUILDKITE_SOURCE_BAZEL"])
    expected = "bazel " + Path(".bazelversion").read_text().strip()
    data = {"path": str(client), "sha256": sha(client), "go_on_path": bool(shutil.which("go")),
            "version": capture([client, "--version"]), "expected": expected,
            "version_override_cleared": "USE_BAZEL_VERSION" not in os.environ,
            "fallback_policy": os.environ.get("USE_BAZEL_FALLBACK_VERSION"),
            "dir": os.environ.get("BUILDKITE_BAZEL_DIRECTORY", ""),
            "make_path_matches": shutil.which("bazel") == str(client)}
    path = WORK / (name + ".payload")
    path.write_text(json.dumps(data))
    if data["version"] != expected:
        return 73
    if name in ("go_only", "wrong_with_go"):
        meta = capture([(WORK / "go-path").read_text().strip(), "version", "-m", client])
        data["pinned_module"] = bool(re.search(r"(?m)^\s*mod\s+github.com/bazelbuild/bazelisk\s+v1\.30\.0(?:\s|$)", meta))
    data["pid"] = int(capture([client, "info", "server_pid"]))
    assert Path("/proc/%d" % data["pid"]).exists()
    path.write_text(json.dumps(data))
    return 0


def environment(name):
    _, have_launcher, plain, have_go, _ = CASES[name]
    allowed = WORK / ("path-" + name)
    allowed.mkdir()
    for tool in UTILS:
        src = shutil.which(tool, path="/usr/local/bin:/usr/bin:/bin")
        if src:
            (allowed / tool).symlink_to(src)
    for tool in [*DENIED, "buildkite-agent"]:
        (allowed / tool).symlink_to(WORK / "dispatch.py")
    if have_launcher:
        (allowed / "bazelisk").symlink_to(WORK / "bazelisk")
    if plain:
        (allowed / "bazel").symlink_to(WORK / BINARY[plain])
    if have_go:
        (allowed / "go").symlink_to((WORK / "go-path").read_text().strip())
    caller = WORK / ("caller-" + name)
    caller.mkdir()
    (caller / "bazel").write_bytes(b"caller owned\n")
    temp = WORK / ("tmp-" + name)
    temp.mkdir()
    uid = "00000000-0000-4000-8000-000000000001"
    proxy = (WORK / "go-proxy").read_text().strip()
    return {"PATH": str(allowed), "HOME": os.environ["HOME"], "USER": os.environ["USER"],
            "LANG": "C.UTF-8", "TMPDIR": str(temp), "HOOK_TASK": str(WORK), "HOOK_CASE": name,
            "GOPROXY": "file://" + proxy, "GOSUMDB": "off", "GOTOOLCHAIN": "local", "CGO_ENABLED": "0",
            "BUILDKITE_AGENT_ENDPOINT": "http://127.0.0.1:1", "BUILDKITE_BUILD_CHECKOUT_PATH": os.getcwd(),
            "BUILDKITE_ORGANIZATION_SLUG": "offline", "BUILDKITE_PIPELINE_SLUG": "source",
            "BUILDKITE_PIPELINE_NAME": "Offline", "BUILDKITE_REPO": os.getcwd(),
            "BUILDKITE_AGENT_NAME": "offline-agent", "BUILDKITE_PIPELINE_PROVIDER": "github",
            "BUILDKITE_BRANCH": "test/coordinator-qualification", "BUILDKITE_JOB_ID": uid,
            "BUILDKITE_BUILD_ID": uid, "BUILDKITE_LABEL": "Offline hook fixture",
            "BUILDKITE_AGENT_META_DATA_QUEUE": "offline", "BUILDKITE_BIN_PATH": str(allowed),
            "BUILDKITE_BAZEL_DIRECTORY": str(caller), "USE_BAZEL_VERSION": "8.5.0", "BUILDKITE_COMMIT": capture(["git", "rev-parse", "HEAD"])}


def run_case(name, out):
    group, installed, plain, have_go, expected = CASES[name]
    env = environment(name)
    assert env["BUILDKITE_COMMIT"] == (FIXED if group == "fixed" else BASE)
    cmd = [str(WORK / "agent"), "pipeline", "upload", "--dry-run", "--format", "json",
           "--agent-access-token", "offline", ".buildkite/pipeline.yaml"]
    step, = [s for s in json.loads(capture(cmd, env=env))["steps"] if isinstance(s, dict) and s.get("key") == "rbe-source"]
    assert step["command"] == '"${BUILDKITE_SOURCE_BAZEL}" test --config=source-presubmit-mixed'
    out["command"] = step["command"]
    env["BUILDKITE_STEP_KEY"] = step["key"]
    payload_cmd = shlex.join(["/usr/bin/python3", "-IB", str(WORK / "client.py"), "payload"])
    cmd = [str(WORK / "agent"), "bootstrap", "--phases", "command", "--shell", "/bin/bash -e -c"]
    env["BUILDKITE_COMMAND"] = payload_cmd
    if name == "governance_bazel_name":
        # Exercise the shared helper as called from the native Make branch. Its
        # unrelated Docker/package hooks are covered by the earlier native job.
        env["BUILDKITE_STEP_KEY"] = "governance"
        cmd = ["/bin/bash", "-exc", "source .buildkite/hooks/libhook; prepare_agent_bazel; " +
               payload_cmd + "; shutdown_agent_bazel"]
    p = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=250)
    log = p.stdout.decode(errors="replace")
    out.update(exit=p.returncode, log_sha256=hashlib.sha256(p.stdout).hexdigest(), diagnostics=safe_line(log),
               ignored_wrong="expected 'bazel 9.3.1', got 'bazel 8.5.0'" in log)
    calls = WORK / (name + ".jsonl")
    events = [json.loads(l) for l in calls.read_text().splitlines()] if calls.exists() else []
    assert not any("forbidden" in e or "unexpected_agent_call" in e for e in events)
    if name != "governance_bazel_name":
        assert any(e.get("offline_download") for e in events)
    observed = WORK / (name + ".payload")
    out["caller_bazel_intact"] = (Path(env["BUILDKITE_BAZEL_DIRECTORY"]) / "bazel").read_bytes() == b"caller owned\n"
    assert out["caller_bazel_intact"] and p.returncode == expected
    if name in ("stock_missing", "wrong_without_go"):
        assert not observed.exists()
        out["cleanup_complete"] = not list(Path(env["TMPDIR"]).glob("tmp.*"))
        assert out["cleanup_complete"]
        if name == "wrong_without_go":
            assert out["ignored_wrong"] and "go: command not found" in log
        return
    d = json.loads(observed.read_text())
    path = Path(d.pop("path"))
    directory = d.pop("dir")
    out.update(d)
    out["selected_name"] = path.name
    out["selected_from_job_dir"] = bool(directory) and path.parent == Path(directory)
    out["shutdown_same_path"] = bool(re.search(r"(?m)^.*\+ " + re.escape(str(path)) + r" shutdown\s*$", log))
    out["cleanup_complete"] = not directory or not Path(directory).exists()
    assert out["shutdown_same_path"] and (group == "stock" or out["cleanup_complete"])
    assert have_go == d["go_on_path"] and d["version_override_cleared"] and d["fallback_policy"] == "error:"
    if name == "stock_wrong":
        assert d["version"] == "bazel 8.5.0" and d["sha256"] == sha(WORK / BINARY["wrong"])
        return
    assert d["version"] == d["expected"] == "bazel 9.3.1"
    pid = Path("/proc") / str(d["pid"]) / "stat"
    out["stopped"] = not pid.exists() or pid.read_text().split(") ", 1)[1].startswith("Z ")
    assert out["stopped"]
    if name in ("go_only", "wrong_with_go"):
        assert directory and d["pinned_module"] and d["make_path_matches"]
    else:
        assert d["sha256"] == sha(WORK / ("bazelisk" if installed else BINARY["matching"]))
        assert bool(directory) == (name == "governance_bazel_name")
        if directory:
            assert d["make_path_matches"]
    if name == "wrong_with_go":
        assert out["ignored_wrong"]


if sys.argv[1] == "payload":
    sys.exit(payload())
status = Path("/proc/self/status").read_text()
assert os.geteuid() != 0 and re.search(r"^NoNewPrivs:\s+1$", status, re.M)
assert all(re.search(r"^" + cap + r":\s+0+$", status, re.M) for cap in ("CapEff", "CapBnd"))
assert {n for _, n in socket.if_nameindex()} == {"lo"} and not Path("/run/docker.sock").exists()
rows = {}
for case, spec in CASES.items():
    if spec[0] != sys.argv[1]:
        continue
    row = rows[case] = {"passed": False, "source": FIXED if spec[0] == "fixed" else BASE}
    try:
        run_case(case, row)
        row["passed"] = True
    except (AssertionError, OSError, ValueError, subprocess.SubprocessError) as exc:
        import traceback
        row["error"] = type(exc).__name__
        row["line"] = traceback.extract_tb(exc.__traceback__)[-1].lineno
    (WORK / "results" / (sys.argv[1] + ".json")).write_text(json.dumps(rows, indent=2) + "\n")
sys.exit(0 if all(r["passed"] for r in rows.values()) else 1)
