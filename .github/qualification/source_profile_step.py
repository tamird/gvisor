"""Render frozen pipelines with the released Buildkite agent and collect one local Bazel test result."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from urllib.parse import unquote, urlparse


BASE = "353c484b45fec1ba5ba19445ba03807a5f9638e0"
PRODUCT = "b3e6b0409ad2b1609a519b58e30b774664b1457f"
PIPE = ".buildkite/rbe-source.yaml"
OWNER = "//test/rbe/source_profiles_qualification:coordinator_test"
AGENT = "f7ecf75a3be41a786f25e9cdbff332dc49b7f18ef63a1fd73bf77056fd4d7fe0"
ROOT = Path(os.environ["RUNNER_TEMP"], "ordinary-source-profiles")
OUT = ROOT / "results"


def git(*args):
    return subprocess.check_output(["git", *args], text=True)


def render(agent, label, yaml, interpolate):
    src = ROOT / (label + ".yaml")
    src.write_text(yaml)
    args = [str(agent), "pipeline", "upload", "--dry-run", "--format", "json"]
    if not interpolate:
        args.append("--no-interpolation")
    env = {"HOME": str(ROOT), "PATH": "/usr/bin:/bin", "failed": "47", "BUILDKITE_AGENT_ENDPOINT": "http://127.0.0.1:1"}
    proc = subprocess.run([*args, str(src)], env=env, text=True, capture_output=True, timeout=15, check=True)
    [step] = json.loads(proc.stdout)["steps"]
    assert step["label"] == "Remote source presubmit" and isinstance(step["command"], str)
    return step["command"]


def main():
    assert git("rev-parse", "HEAD^").strip() == PRODUCT
    assert git("rev-parse", "HEAD^^").strip() == BASE
    assert git("diff", "--name-only", BASE, PRODUCT).splitlines() == [PIPE]
    base, new = (git("show", commit + ":" + PIPE) for commit in (BASE, PRODUCT))
    cfg = git("show", PRODUCT + ":.bazelrc")
    for profile in ("source-presubmit", "source-presubmit-arm64"):
        assert "build:" + profile + " --config=source-presubmit-common" in cfg
    assert "test:source-presubmit-common --target_pattern_file=test/rbe/source-presubmit.targets" in cfg
    data = ROOT / "agent.tar.gz"
    assert hashlib.sha256(data.read_bytes()).hexdigest() == AGENT
    agent = ROOT / "buildkite-agent"
    with tarfile.open(data, "r:gz") as archive:
        [member] = [m for m in archive.getmembers() if m.isfile() and Path(m.name).name == "buildkite-agent"]
        with archive.extractfile(member) as source:
            agent.write_bytes(source.read())
    agent.chmod(0o755)
    assert new.count("$$failed") == 1
    expressions = {
        "baseline": render(agent, "baseline", base, True),
        "candidate": render(agent, "candidate", new, True),
        "without_interpolation": render(agent, "literal", new, False),
        "unescaped": render(agent, "unescaped", new.replace("$$failed", "$failed"), True),
    }
    assert expressions["without_interpolation"].replace("$$failed", "$failed") == expressions["candidate"]
    assert expressions["candidate"].replace("$failed", "47") == expressions["unescaped"]
    receipt = {"base": BASE, "source": PRODUCT, "buildkite_agent": "4.10.1",
               "agent_archive_sha256": AGENT, "uploaded": False, "commands": expressions}
    report = OUT / "result.json"
    report.write_text(json.dumps(receipt, indent=1) + "\n")
    env = dict(os.environ)
    names = []
    for key in ("baseline", "candidate", "unescaped"):
        name = "RENDERED_" + key.upper()
        env[name] = expressions[key]
        names.append("--test_env=" + name)
    bep = ROOT / "test.bep"
    args = [str(ROOT / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g",
            "test", OWNER, "--remote_executor=", "--remote_cache=", "--bes_backend=",
            "--spawn_strategy=processwrapper-sandbox", "--strategy=TestRunner=processwrapper-sandbox",
            "--jobs=3", "--nocache_test_results", "--flaky_test_attempts=1", "--test_timeout=45",
            "--build_event_json_file=" + str(bep), *names]
    rc = subprocess.run(args, env=env).returncode
    receipt["bazel_exit"] = rc
    results = []
    if bep.is_file():
        for line in bep.open():
            event = json.loads(line)
            if "testResult" not in event:
                continue
            assert event["id"]["testResult"]["label"] == OWNER
            t = event["testResult"]
            result = {"owner": OWNER, "status": t["status"], "strategy": t.get("executionInfo", {}).get("strategy"),
                      "cached": bool(t.get("cachedLocally", False) or t.get("executionInfo", {}).get("cachedRemotely", False))}
            results.append(result)
            [log] = [v for v in t["testActionOutput"] if v["name"] == "test.log"]
            uri = urlparse(log["uri"])
            assert uri.scheme == "file"
            contents = Path(unquote(uri.path)).read_bytes()
            result["log_sha256"] = hashlib.sha256(contents).hexdigest()
            matches = [s.removeprefix("PROFILE_RESULTS ") for s in contents.decode().splitlines() if s.startswith("PROFILE_RESULTS ")]
            if len(matches) == 1:
                result["observed"] = json.loads(matches[0])
    receipt["test_results"] = results
    receipt["verified"] = (rc == 0 and len(results) == 1 and results[0]["status"] == "PASSED" and not results[0]["cached"]
                           and results[0]["strategy"] == "processwrapper-sandbox" and "observed" in results[0])
    report.write_text(json.dumps(receipt, indent=1) + "\n")
    if not receipt["verified"]:
        sys.exit("incomplete original Bazel result")


if __name__ == "__main__":
    main()
