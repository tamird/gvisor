"""Check the combined native Buildkite jobs and exercise the ARM64 license owner."""

import datetime
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import re
import shlex
import subprocess
from threading import Thread
from zipfile import ZipFile
import xml.etree.ElementTree as ET

import yaml

import qualification_report as report


TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "unit-results"
SOURCE = os.environ["PRODUCT"]
OWNER = "//tools/licensecheck:licensecheck_test"
RESULT: dict[str, object] = {"status": "INCOMPLETE", "source": SOURCE}


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args])


def save() -> None:
    report.write_json(OUT / "verdict.json", RESULT)


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


NOGO = os.environ["NOGO"]
UNIT = os.environ["UNIT"]
PARENT = os.environ["PARENT"]
METADATA = "--build_metadata=AGENT_PROBE=quoted cache option"
FILES = [".buildkite/first-pass.yaml", ".buildkite/pipeline.yaml"]


class Cache(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/ac/"):
            self.server.ac_reads += 1
        self.send_error(404)

    def log_message(self, *args):
        pass


def original_nogo():
    original = TMP / "original-nogo.zip"
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    assert digest == "6d8701fe3ea2b7a3623ebef38c43b3850ce0c196113bb609af97a449654fe4d8"
    with ZipFile(original) as z:
        summary = json.loads(z.read("verdict.json"))
        good = json.loads(z.read("clean.json"))
        bad = json.loads(z.read("bad.json"))
        hook = json.loads(z.read("post-command.json"))
    assert summary["source"] == NOGO and summary["verified"] and not summary["builder"]
    assert good["agent_options"] and good["bazel_exit"] == [0] and good["exit"] == 0
    assert len(good["executions"]) == 3 and set(good["executions"].values()) == {"PASSED"}
    assert all(v == {"amd64": True, "arm64": True} for v in good["architectures"].values())
    assert bad["agent_options"] and bad["bazel_exit"] == [3] and bad["exit"] == 2
    assert bad["executions"][hook["target"]] == "FAILED" and hook["diagnostic"] == "self-assignment of v to v"
    RESULT["prior_nogo"] = {"artifact": 13205005426, "sha256": digest, **summary,
                            "pass_invocation": good["invocation"], "failure_invocation": bad["invocation"],
                            "clean_owners": good["executions"], "negative": hook}


def pipelines():
    for ref in (PARENT, NOGO, UNIT):
        assert git("diff", "--name-only", ref, SOURCE).decode().splitlines() == FILES
    assert git("rev-parse", "HEAD^").decode().strip() == PARENT
    selected = []
    unrelated = {}
    for path in FILES:
        docs = [yaml.safe_load(git("show", f"{rev}:{path}")) for rev in (SOURCE, PARENT, NOGO, UNIT)]
        current = docs[0]
        assert all({k: v for k, v in d.items() if k != "steps"} ==
                   {k: v for k, v in current.items() if k != "steps"} for d in docs[1:])
        others = licensed = 0
        for step, old, n, u in zip(*(d["steps"] for d in docs), strict=True):
            cmd = step.get("command") if isinstance(step, dict) else None
            if cmd not in ("make unit-tests", "make nogo-tests"):
                assert step == old
                if cmd == "make license-check":
                    assert step["env"]["DOCKER_BUILD"] == "false"
                    licensed += 1
                others += 1
                continue
            sibling = n if cmd == "make nogo-tests" else u
            assert step == sibling and step == old | {"env": {"DOCKER_BUILD": "false"}}
            selected.append({"pipeline": path, "label": step["label"], "command": cmd, "env": step["env"],
                             "agents": current.get("agents", {}) | step.get("agents", {}), "condition": step["if"]})
        assert licensed == 1
        unrelated[path] = others
    assert len(selected) == 6 and sum(s["command"] == "make nogo-tests" for s in selected) == 2
    assert {s["agents"].get("cgroup") for s in selected if s["command"] == "make unit-tests"} == {None, "v1", "v2"}
    RESULT.update(selected_jobs=selected, unchanged_steps=unrelated, siblings={"unit": UNIT, "nogo": NOGO, "parent": PARENT},
                  identical_except=FILES)
    return selected


def make_arguments(selected, cgroup, cache_url):
    bindir = TMP / "guard-bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text('#!/bin/sh\ntouch "$RUNNER_TEMP/docker-called"\nexit 91\n')
    docker.chmod(0o755)
    recorder = TMP / "record-bazel"
    recorder.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$RUNNER_TEMP/make-arguments"\n')
    recorder.chmod(0o755)
    env = dict(os.environ, PATH=str(bindir) + ":" + os.environ["PATH"],
               BAZEL_REMOTE_CACHE=f"--remote_cache={cache_url} --remote_upload_local_results=false " + shlex.quote(METADATA),
               DOCKER_HOST="unix:///nonexistent/qualification-daemon.sock")
    env.pop("DOCKER_BUILD", None)
    args = {}
    for step in selected:
        command = step["command"]
        before = TMP / "make-arguments"
        before.unlink(missing_ok=True)
        subprocess.run([*shlex.split(command), f"BAZEL={recorder}"], env=env | step["env"], check=True)
        current = before.read_text().splitlines()
        assert current[0] == "test" and all(current.count(arg) == 1 for arg in
            (METADATA, f"--remote_cache={cache_url}", "--remote_upload_local_results=false"))
        assert not (TMP / "docker-called").exists()
        if command == "make nogo-tests":
            assert current[-3:] == ["--test_tag_filters=nogo", "--build_tests_only", "//..."]
        else:
            assert current[current.index("--")+1:] == ["//:all", "pkg/...", "tools/...", "runsc/...", "vdso/...", "sandboxexec/...", "examples/...", "test/trace/..."]
            assert all(current.count(arg) == 1 for arg in
                (f"--test_env=CGROUPV2={cgroup}", "--test_tag_filters=-nogo,-requires-kvm", "--build_tag_filters=-network_plugins"))
        assert command not in args or args[command] == current
        args[command] = current
    original = args["make unit-tests"]
    opposite = "false" if cgroup == "true" else "true"
    subprocess.run(["make", "unit-tests", f"BAZEL={recorder}", f"CGROUPV2={opposite}"],
                   env=env | {"DOCKER_BUILD": "false"}, check=True)
    variant = (TMP / "make-arguments").read_text().splitlines()
    assert [a.replace(f"--test_env=CGROUPV2={cgroup}", f"--test_env=CGROUPV2={opposite}") for a in original] == variant
    assert not (TMP / "docker-called").exists()
    RESULT.update(make_arguments=args, docker_calls=0, cgroup_control={"native": cgroup, "command_override_only": opposite})
    save()
    return original


def main() -> None:
    assert git("rev-parse", "HEAD").decode().strip() == SOURCE
    assert platform.machine() == "aarch64", platform.machine()
    assert os.getuid() != 0, "Expected the ordinary hosted runner user"
    cgroup = "true" if Path("/sys/fs/cgroup/cgroup.controllers").is_file() else "false"
    RESULT["host"] = {"architecture": platform.machine(), "kernel": platform.release(),
                      "uid": os.getuid(), "cpus": os.cpu_count(), "cgroup_v2": cgroup,
                      "memory": next(line for line in Path("/proc/meminfo").read_text().splitlines()
                                     if line.startswith("MemTotal:")),
                      "apparmor_restrict_unprivileged_userns": Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns").read_text().strip()}
    selected = pipelines()
    original_nogo()
    cache = ThreadingHTTPServer(("127.0.0.1", 0), Cache)
    cache.ac_reads = 0
    Thread(target=cache.serve_forever, daemon=True).start()
    cache_url = f"http://127.0.0.1:{cache.server_port}"
    original = make_arguments(selected, cgroup, cache_url)
    separator = original.index("--")
    args = original[:separator] + ["--test_env=GO_TEST_WRAP_TESTV=1", "--nocache_test_results",
                                  "--flaky_test_attempts=1", "--runs_per_test=1",
                                  f"--build_event_json_file={TMP / 'raw.jsonl'}", "--", OWNER]
    directory = TMP / "owner"
    directory.mkdir()
    (directory / "executed_arguments").write_text("\n".join(args) + "\n")
    (directory / "commit_file").write_bytes(git("cat-file", "commit", SOURCE))
    (directory / "source_before").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    (TMP / "owners").write_text(OWNER + "\n")
    RESULT["started_at"] = now()
    save()
    with (TMP / "console").open("w") as console:
        try:
            result = subprocess.run(["bazel", f"--bazelrc={TMP / 'unit-resources.bazelrc'}", *args],
                                    stdout=console, stderr=subprocess.STDOUT, timeout=2400)
            native_exit = result.returncode
        except subprocess.TimeoutExpired:
            native_exit = 124
    RESULT.update(native_exit=native_exit, execution_finished_at=now())
    save()
    (directory / "native_exit").write_text(str(native_exit))
    (directory / "source_after").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    (directory / "final_head").write_bytes(git("rev-parse", "HEAD"))
    if native_exit:
        lines = (TMP / "console").read_text(errors="replace").splitlines()
        RESULT["diagnostics"] = [line[:600] for line in lines if re.match(r"^(ERROR:|FATAL:|FAIL:|FAIL\t)", line)][:20]
        save()
    events = directory / "events.jsonl"
    captured = report.capture(TMP / "raw.jsonl", events, Path("bazel-out"), directory / "reports")
    (directory / "capture_exit").write_text("1" if captured["errors"] else "0")
    keys = ("executed_arguments", "commit_file", "source_before", "source_after", "native_exit", "capture_exit", "final_head")
    context = {key: key for key in keys}
    context.update(expected_source=SOURCE, command_kind="test", execution_finished_at=RESULT["execution_finished_at"])
    report.write_json(directory / "context.json", context)
    verdict = report.verdict(events, report.manifest_path(events), directory / "context.json", TMP / "owners")
    RESULT["owner_verdict"] = verdict
    save()
    with (TMP / "raw.jsonl").open() as raw:
        from_options = [e["optionsParsed"]["explicitCmdLine"] for line in raw
                        if "optionsParsed" in (e := json.loads(line))]
    assert len(from_options) == 1
    required = [f"--remote_cache={cache_url}", "--remote_upload_local_results=false", METADATA, f"--test_env=CGROUPV2={cgroup}"]
    RESULT["actual_bazel_flags"] = {flag: from_options[0].count(flag) for flag in required}
    RESULT["actual_bazel_flags"]["--remote_upload_local_results=false"] += from_options[0].count("--noremote_upload_local_results")
    RESULT["cache_action_reads"] = cache.ac_reads
    RESULT["capture"] = captured
    save()
    for ext in ("xml", "log"):
        source, = (directory / "reports").rglob("test." + ext)
        (OUT / ("test." + ext)).write_bytes(source.read_bytes())
    policy, = [case for case in ET.parse(OUT / "test.xml").iter("testcase") if case.get("name") == "TestLicensePolicy"]
    RESULT["policy_case"] = dict(policy.attrib)
    assert all(policy.find(child) is None for child in ("failure", "error", "skipped")), ET.tostring(policy).decode()
    assert policy.get("status") != "notrun" and policy.get("result") != "suppressed", policy.attrib
    attempt, = verdict["executed"]
    assert attempt["owner"]["label"] == OWNER and all(attempt[key] == 1 for key in ("run", "shard", "attempt")), attempt
    assert not attempt["cached_locally"] and not attempt["execution_info"].get("cachedRemotely"), attempt
    assert attempt["execution_info"].get("strategy") in ("local", "processwrapper-sandbox", "linux-sandbox", "sandboxed"), attempt
    assert verdict["status"] == "PASS" and not verdict["zero_case_owners"] and not verdict["all_skipped_owners"], verdict
    assert all(value == 1 for value in RESULT["actual_bazel_flags"].values()) and cache.ac_reads > 0
    RESULT["status"] = "PASS"


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        RESULT["status"] = "FAIL" if RESULT.get("native_exit", 0) else "INCOMPLETE"
        RESULT["verification_error"] = f"{type(error).__name__}: {error}"
    RESULT["verdict_available_at"] = now()
    save()
    raise SystemExit(0 if RESULT["status"] == "PASS" else 1)
