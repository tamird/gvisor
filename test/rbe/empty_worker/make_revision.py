"""Check the Make registry revision and run its existing Linux smoke test."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlparse

workspace = Path(os.environ["GITHUB_WORKSPACE"])
tmp = Path(os.environ["RUNNER_TEMP"])
out = tmp / "package-evidence"
registry = workspace / "registry"
revision = "e00baea8db3a686e45379b9096998a8cb95291c0"
product = "7dc8df3c4bb7da1bc04037fe9a2d641cb36cc5a4"
mode = sys.argv[1]
dest = out / ("supplier.json" if mode == "checks" else "smoke.json")
row = {"status": "INCOMPLETE", "registry_source": revision, "product": product,
       "mode": mode, "phases": {}, "executed_cases": []}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save():
    dest.write_text(json.dumps(row, indent=2) + "\n")


def invoke(name, command, cwd):
    phase = {"started": now()}
    row["phases"][name] = phase
    save()
    with (tmp / ("make-" + name + ".log")).open("w") as stream:
        phase["exit"] = subprocess.run(
            ["timeout", "--signal=INT", "--kill-after=25s", "18m", *command],
            cwd=cwd, stdout=stream, stderr=subprocess.STDOUT).returncode
    phase["finished"] = now()
    errors = []
    for line in (tmp / ("make-" + name + ".log")).read_text(errors="replace").splitlines():
        if name == "buildifier" and "# reformat" in line:
            errors.append(Path(line.split()[0]).name + " requires formatting")
        elif line.startswith(("ERROR:", "FATAL:", "Error:")):
            kinds = ["no such package", "no such target", "no such repository", "checksum mismatch", "failed to build", "failed to fetch"]
            errors.append(next((kind for kind in kinds if kind in line.lower()), "tool reported an error"))
        if len(errors) >= 5:
            break
    phase["errors"] = errors
    save()
    return phase["exit"]


save()
try:
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=registry, text=True).strip() == revision
    bazel = ["bazel", "--bazelrc=" + str(tmp / "sidecar.bazelrc")]
    if mode == "checks":
        entry = registry / "modules/make/4.4.1.bcr.4"
        files = ["MODULE.bazel", "decisions.bzl", "overlay/BUILD.bazel", "overlay/MODULE.bazel",
                 "overlay/REPO.bazel", "overlay/autoconf.bzl", "overlay/tests/BUILD.bazel"]
        invoke("buildifier", [*bazel, "run", "@buildifier_prebuilt//:buildifier", "--",
                             "-mode=check", *[str(entry / name) for name in files]], workspace / "carrier")
        invoke("registry", [*bazel, "run", "//tools:bcr_validation", "--",
                            "--check=make@4.4.1.bcr.4"], registry)
        row["source_clean"] = subprocess.run(["git", "diff", "--quiet", revision, "--"], cwd=registry).returncode == 0
        row["status"] = "PASS" if row["source_clean"] and all(p["exit"] == 0 for p in row["phases"].values()) else "FAIL"
    else:
        assert mode == "smoke"
        cwd = workspace / "product"
        assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=cwd, text=True).strip() == product
        bep = tmp / "make-smoke.bep"
        target = "@@make+//tests:make_smoke_test"
        flags = ["--config=x86_64", "--config=plugin-tldk", "--compilation_mode=opt",
                 "--host_platform=//test/rbe/empty_worker:amd64_musl",
                 "--extra_execution_platforms=//test/rbe/empty_worker:amd64_musl",
                 "--registry=" + registry.as_uri(), "--registry=https://bcr.bazel.build",
                 "--jobs=400", "--strategy=TestRunner=local", "--nocache_test_results",
                 "--remote_download_outputs=all", "--build_event_json_file=" + str(bep)]
        rc = invoke("smoke", [*bazel, "test", *flags, target], cwd)
        row["execution"] = "Unchanged module smoke on the populated Ubuntu coordinator; builds use remote execution. Not a scratch runtime claim."
        results = []
        if bep.exists():
            for line in bep.read_text().splitlines():
                event = json.loads(line)
                if "started" in event:
                    row["invocation"] = event["started"].get("uuid")
                label = event["id"].get("testResult", {}).get("label", "")
                if label.endswith("//tests:make_smoke_test"):
                    results.append(event["testResult"])
        assert len(results) == 1
        result = results[0]
        row["test_status"] = result.get("status")
        row["cached_locally"] = result.get("cachedLocally", False)
        row["execution_info"] = {key: result.get("executionInfo", {}).get(key) for key in ["strategy", "cachedRemotely"]}
        logs = [f for f in result.get("testActionOutput", []) if f.get("name") == "test.log"]
        assert len(logs) == 1
        uri = urlparse(logs[0]["uri"])
        assert uri.scheme == "file" and not uri.netloc
        data = Path(unquote(uri.path)).read_bytes()
        row["test_log_sha256"] = hashlib.sha256(data).hexdigest()
        text = data.decode()
        names = re.findall(r"^\[PASS\] ([a-z_]+)$", text, re.M)
        row["executed_cases"] = names
        expected = ["version", "hello", "substref", "functions", "fail_propagation", "parallel"]
        assert names == expected and re.search(r"^6/6 tests passed$", text, re.M)
        row["status"] = "PASS" if rc == 0 and result.get("status") == "PASSED" and not row["cached_locally"] and not row["execution_info"]["cachedRemotely"] else "FAIL"
except Exception as err:
    row["collection_error_type"] = type(err).__name__
    if any(p.get("exit", 0) != 0 for p in row["phases"].values()):
        row["status"] = "FAIL"
finally:
    row["verified_at"] = now()
    save()
print(mode, row["status"], flush=True)
sys.exit(row["status"] != "PASS")
