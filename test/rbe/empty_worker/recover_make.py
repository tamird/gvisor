"""Project original Make/LLVM failures without rerunning any build or test."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request
import uuid

root = Path(os.environ["RUNNER_TEMP"])
out = root / "package-evidence"
out.mkdir(exist_ok=True)
row = {"status": "INCOMPLETE", "executed_targets": 0, "diagnostics": {}}
ansi = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
source_error = re.compile(r"^(?:external|bazel-out)/[A-Za-z0-9_+./-]+:\d+(?::\d+)?: (?:fatal )?error: .*$")
driver_error = re.compile(r"^(?:[A-Za-z0-9_+./-]+/)?(?:clang(?:\+\+)?|llvm-ar): (?:fatal )?error: (?:unknown argument|unsupported option|no such file or directory|cannot specify|unable to make temporary file).*", re.I)
causes = {
    "missing_file": r"(?:No such file or directory|errno[ =]+2\b)",
    "permission_denied": r"Permission denied",
    "executable_format": r"Exec format error",
    "read_only_filesystem": r"Read-only file system",
    "disk_full": r"No space left on device",
    "temporary_file": r"(?:unable|failed|cannot) (?:to )?(?:make|create|open) (?:a )?temp(?:orary)?(?: file| directory)",
    "unavailable": r"(?:code[ =]+Unavailable|\bUNAVAILABLE:)",
    "missing_runtime_library": r"error (?:while )?loading shared libraries|GLIBC_[0-9.]+[^\n]*not found",
    "unknown_bazel_command": r"Unknown command",
    "original_stderr_unavailable": r"Failed to download failed action stderr",
}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def project(text):
    clean = ansi.sub("", text)
    errors = [line for line in clean.splitlines() if "\x1b" not in line and (source_error.fullmatch(line) or driver_error.fullmatch(line))][:8]
    return {"compiler_errors": errors, "failure_signatures": [name for name, pattern in causes.items() if re.search(pattern, clean, re.I)], "bytes": len(text.encode()), "ansi_sequences": len(ansi.findall(text)), "sha256": hashlib.sha256(text.encode()).hexdigest()}


def save():
    (out / "recovery.json").write_text(json.dumps(row, indent=2) + "\n")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def lookup_coordinator():
    source = "050a46df43958c91c4b6cfde8c902b5e6a18088d"
    result = {"status": "INCOMPLETE", "source": source, "started": now()}
    try:
        request = urllib.request.Request(
            "https://app.buildbuddy.io/api/v1/GetInvocation",
            data=json.dumps({"selector": {"commit_sha": source}}).encode(),
            headers={"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]},
        )
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=60) as response:
            data = json.load(response)
        result["invocations"] = []
        for item in data.get("invocation", []):
            identifier = item.get("id", {}).get("invocationId", item.get("id", {}).get("invocation_id"))
            pattern = item.get("pattern", "")
            assert item.get("commitSha", item.get("commit_sha")) == source
            assert not pattern or all(token.startswith(("//", "@")) for token in pattern.split())
            assert os.environ["BUILDBUDDY_API_KEY"] not in pattern
            result["invocations"].append({
                "id": str(uuid.UUID(identifier)), "target": pattern,
                "success": item.get("success", False),
                "status": item.get("invocationStatus"),
                "bazel_exit_code": item.get("bazelExitCode", item.get("bazel_exit_code", "")),
            })
        result["has_more"] = bool(data.get("nextPageToken", data.get("next_page_token")))
        result["status"] = "RECOVERED"
    except Exception as err:
        result["error_type"] = type(err).__name__
    result["finished"] = now()
    (out / "coordinator-lookup.json").write_text(json.dumps(result, indent=2) + "\n")


lookup_coordinator()

save()
try:
    assert project("\x1b[1mexternal/make+/src/main.c:3:4: \x1b[31merror: example\x1b[0m")["compiler_errors"] == ["external/make+/src/main.c:3:4: error: example"]
    assert not project("build --remote_header=x-buildbuddy-api-key=example")["compiler_errors"]
    for name, run, invocation in [
        ("scratch", 41318538648, "19a19c35-4120-417d-b05d-d85b9d68ee22"),
        ("smoke", 41318538648, "0f72a7f1-0dc1-4604-a6c0-9e80a7554873"),
        ("populated-before", 41319479188, "7bb496b3-ca24-41f4-a069-b4a30c861c94"),
        ("populated-after", 41319479188, "df6c0d5d-2027-4ce7-b9d9-cb4ba7b75cbe"),
    ]:
        item = {"original_run": run, "invocation": invocation, "started": now(), "reads": []}
        row["diagnostics"][name] = item
        for mode, flags in [("first-error", ["--errors"]), ("build-log", ["--lines=2000"])]:
            path = root / (name + "-" + mode + ".private")
            stderr = root / (name + "-" + mode + "-stderr.private")
            with path.open("w") as stream, stderr.open("w") as err:
                code = subprocess.run([str(root / "bb"), "view", invocation, *flags], stdout=stream, stderr=err, timeout=120).returncode
            part = project(path.read_text(errors="replace"))
            part.update({"mode": mode, "exit": code, "stderr_signatures": project(stderr.read_text(errors="replace"))["failure_signatures"]})
            item["reads"].append(part)
            save()
            if code == 0 and part["compiler_errors"]:
                break
        item["finished"] = now()
        item["status"] = "RECOVERED" if any(v["exit"] == 0 and (v["compiler_errors"] or v["failure_signatures"]) for v in item["reads"]) else "INCOMPLETE"
        save()
    row["status"] = "PASS" if all(v["status"] == "RECOVERED" for v in row["diagnostics"].values()) else "INCOMPLETE"
except Exception as err:
    row["error_type"] = type(err).__name__
finally:
    row["verified_at"] = now()
    save()
print(row["status"], flush=True)
sys.exit(row["status"] != "PASS")
