"""Recover original compiler errors and separate inherited formatter changes."""
import datetime
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

workspace = Path(os.environ["GITHUB_WORKSPACE"])
tmp = Path(os.environ["RUNNER_TEMP"])
out = tmp / "package-evidence"
registry = workspace / "registry"
row = {"status": "INCOMPLETE", "original_run": 41318538648,
       "original_artifact": 13087519897, "diagnostics": {}, "format": {}}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save():
    (out / "recovery.json").write_text(json.dumps(row, indent=2) + "\n")


save()
try:
    for name, invocation in [("scratch", "19a19c35-4120-417d-b05d-d85b9d68ee22"),
                             ("smoke", "0f72a7f1-0dc1-4604-a6c0-9e80a7554873")]:
        path = tmp / ("original-" + name + ".log")
        item = {"invocation": invocation, "started": now()}
        row["diagnostics"][name] = item
        with path.open("w") as stream, (tmp / (name + "-view.log")).open("w") as err:
            item["exit"] = subprocess.run([str(tmp / "bb"), "view", invocation, "--errors"], stdout=stream, stderr=err, timeout=120).returncode
        item["finished"] = now()
        # Only the compiler's source-location error records, never the Bazel
        # command line, event payload or remote metadata header.
        pattern = r"^(?:external|bazel-out)/[A-Za-z0-9_+./-]+:\d+:\d+: (?:fatal )?error: (.*)$"
        driver = r"^(?:[A-Za-z0-9_+./-]+/)?clang(?:\+\+)?: (?:fatal )?error: (?:unknown argument|unsupported option|no such file or directory|cannot specify).*"
        item["compiler_errors"] = [line for line in path.read_text(errors="replace").splitlines() if re.match(pattern, line) or re.match(driver, line)]
        item["compiler_errors"] = item["compiler_errors"][:8]
        save()
    os.environ.pop("BUILDBUDDY_API_KEY", None)
    for version, name, relative in [
        ("4.4.1.bcr.3", "before-main", "overlay/BUILD.bazel"),
        ("4.4.1.bcr.4", "after-main", "overlay/BUILD.bazel"),
        ("4.4.1.bcr.3", "before-tests", "overlay/tests/BUILD.bazel"),
        ("4.4.1.bcr.4", "after-tests", "overlay/tests/BUILD.bazel"),
    ]:
        source = registry / "modules/make" / version / relative
        output = tmp / (name + "-format") / "BUILD.bazel"
        output.parent.mkdir(exist_ok=True)
        shutil.copyfile(source, output)
        item = {"started": now(), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
        row["format"][name] = item
        command = ["bazel", "--bazelrc=" + str(tmp / "sidecar.bazelrc"), "run", "@buildifier_prebuilt//:buildifier", "--", "-mode=fix", str(output)]
        with (tmp / (name + "-format.log")).open("w") as stream:
            item["exit"] = subprocess.run(command, cwd=workspace / "carrier", stdout=stream, stderr=subprocess.STDOUT, timeout=180).returncode
        item["finished"] = now()
        if item["exit"] == 0:
            text = output.read_text()
            original = source.read_text()
            item["already_formatted"] = text == original
            item["formatted_sha256"] = hashlib.sha256(text.encode()).hexdigest()
            (out / (name + "-BUILD.bazel")).write_text(text)
            (out / (name + "-format.patch")).write_text(''.join(difflib.unified_diff(original.splitlines(True), text.splitlines(True), fromfile=name+'/BUILD.bazel', tofile=name+'/BUILD.bazel')))
        save()
    row["status"] = "PASS" if all(v["exit"] == 0 and v["compiler_errors"] for v in row["diagnostics"].values()) and all(v["exit"] == 0 for v in row["format"].values()) else "INCOMPLETE"
except Exception as err:
    row["error_type"] = type(err).__name__
finally:
    row["verified_at"] = now()
    save()
print(row["status"], flush=True)
sys.exit(row["status"] != "PASS")
