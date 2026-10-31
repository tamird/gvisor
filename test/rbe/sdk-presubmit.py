"""Exercise the supplier's default Linux presubmit and its prebuilt SDK path."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import traceback

here = Path.cwd()
temp = Path(os.environ["RUNNER_TEMP"])
records = temp / "presubmit-records"
records.mkdir(exist_ok=True)
source = json.loads((here / "test/rbe/sdk-presubmit-spec.json").read_text())
repo = temp / "rules_go"
env = dict(os.environ)
results = {}


def checked(argv, cwd):
    return subprocess.check_output(argv, cwd=cwd, text=True, env=env).strip()


def save(name, data):
    (records / name).write_text(json.dumps(data, indent=2) + "\n")


def invoke(name, args, bep=None):
    log = temp / (name + ".log")
    with log.open("w") as stream:
        rc = subprocess.run(args, cwd=repo, env=env, stdout=stream,
                            stderr=subprocess.STDOUT).returncode
    lines = log.read_text(errors="replace").splitlines()
    problems = [l for l in lines if any(k in l for k in (
        "ERROR:", "FAIL:", "(FAILED", "FAILED TO BUILD", "No space left",
        "error: ", "Invocation ID:", "Executed ", "Build completed", "Exception in thread"
    ))]
    (records / (name + "-diagnostics.txt")).write_text(
        "\n".join(problems[-110:] + lines[-18:]) + "\n"
    )
    suites = []
    incomplete = []
    if bep and bep.exists():
        with bep.open() as stream:
            for line in stream:
                event = json.loads(line)
                test = event.get("testSummary")
                if test:
                    suites.append([event["id"]["testSummary"]["label"],
                                   test.get("overallStatus")])
                elif event.get("aborted") and event["aborted"].get("reason"):
                    incomplete.append([event["id"], event["aborted"]])
        save(name + "-tests.json", {"suites": suites, "aborted": incomplete})
    if rc:
        raise RuntimeError(f"{name}: exit {rc}; see diagnostics")
    if bep:
        assert suites and all(s in ("PASSED", "FLAKY") for _, s in suites), suites
    return len(suites)


def remember(name, run):
    try:
        results[name] = {"status": "PASS", "result": run()}
    except Exception:
        results[name] = {"status": "FAIL", "reason": traceback.format_exc()}
    save("verdict.json", {"source": source, "results": results})


def setup():
    repo.mkdir()
    checked(["git", "init", "-q"], repo)
    checked(["git", "fetch", "-q", "--depth=1",
             "https://github.com/bazel-contrib/rules_go.git", source["base"]], repo)
    checked(["git", "checkout", "-q", "--detach", "FETCH_HEAD"], repo)
    checked(["git", "apply", "--index", str(here / "test/rbe/sdk-supplier.patch")], repo)
    assert checked(["git", "write-tree"], repo) == source["tree"]
    env["USE_BAZEL_VERSION"] = (repo / ".bazelversion").read_text().strip()
    version = checked([bazel, "--version"], repo)
    assert version == "bazel " + env["USE_BAZEL_VERSION"], version
    return {"sourceTree": source["tree"], "bazel": version}


def prebuilt():
    target = "//tests/core/stdlib:buildid"
    version = "--//go/toolchain:sdk_version=1.22.12"
    invoke("go1.22-buildid", [bazel, "build", "--jobs=4", version, target])
    paths = checked([bazel, "cquery", version, "--output=files", target], repo).splitlines()
    assert len(paths) == 1, paths
    output = repo / paths[0]
    resolved = output.resolve(strict=True)
    assert resolved.as_posix().endswith("/pkg/tool/linux_amd64/buildid"), str(resolved)
    assert output.is_symlink() and os.access(output, os.X_OK), str(output)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    assert digest == hashlib.sha256(resolved.read_bytes()).hexdigest()
    return {"target": target, "output": paths[0], "sdkFile": str(resolved),
            "sha256": digest, "sdkVersion": "1.22.12"}


def suite(name, args):
    bep = temp / (name + ".bep")
    return invoke(name, [bazel, "test", "--jobs=4", "--keep_going",
                         "--test_output=errors", "--build_event_json_file=" + str(bep), *args], bep)


bazel = shutil.which("bazelisk") or shutil.which("bazel")
remember("setup", setup)
if results["setup"]["status"] == "PASS":
    remember("prebuilt", prebuilt)
    remember("ubuntu2404", lambda: suite("ubuntu2404", ["--config=incompatible", "--", "//...", "-//docs:all"]))
    remember("docs", lambda: suite("docs", ["//docs/..."]))
raise SystemExit(any(r["status"] != "PASS" for r in results.values()))
