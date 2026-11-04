"""Read the one original kernel race build log using the existing fixed-origin helper."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile

from cas_inputs import fetch

ROOT = Path(os.environ["RUNNER_TEMP"])
PUBLIC = ROOT / "race-public"
identifier = "ec927e06-b03e-4103-97b5-fe60c96e9c52"
assert sys.argv[1:] == ["--original-only"]
raw = (ROOT / "original.zip").read_bytes()
digest = hashlib.sha256(raw).hexdigest()
assert digest == "3c63c39edb79de1535951590a711b295b72984ecbb2741831a8d5eb0f4672b16"
with zipfile.ZipFile(io.BytesIO(raw)) as archive:
    assert archive.namelist() == ["result.json"]
    orig = json.loads(archive.read("result.json"))
assert orig["source"] == "97bfc4ae73a4e74751f4db335918970889807bb6"
assert orig["product"] == "8cdb6eb0da66ecf93b0a5e32a2ee970016833a5a"
assert orig["invocations"] == [identifier] and orig["native_exit"] == 1 and orig["executions"] == orig["cases"] == []
result = dict(original=orig, original_zip_sha256=digest, console={}, status="INCOMPLETE", workload_repeated=False)

def save():
    output = json.dumps(result, indent=2, sort_keys=True) + "\n"
    key = os.environ["BUILDBUDDY_API_KEY"]
    assert key and key not in output and json.dumps(key)[1:-1] not in output
    assert len(output.encode()) < 128 * 1024
    (PUBLIC / "verdict.json").write_text(output)

def main():
    # Only words and paths already present in this public, fixed source or in
    # this fixed diagnostic vocabulary may be projected. Omit URLs and lines
    # concerning credentials, command lines or headers.
    public_files = subprocess.check_output(["git","ls-files"],text=True).splitlines()
    paths = set(public_files)
    known = set(re.findall(r"[A-Za-z0-9_+.~-]+", " ".join(public_files)))
    for name in ("MODULE.bazel", ".bazelrc", "pkg/sentry/kernel/BUILD", "pkg/sentry/kernel/fd_table_test.go",
                 "pkg/sentry/kernel/pidfd.go", "pkg/sentry/kernel/task.go", "pkg/sentry/kernel/task_clone.go"):
        known.update(re.findall(r"[A-Za-z0-9_+.~-]+",Path(name).read_text()))
    known.update("""error fatal failed failure while loading resolving downloading compiling linking fetching validating
        module repository package configuration dependency dependencies constraint toolchain platform target action worker
        missing input output file directory not found does not exist cannot create no such path source invalid flag option
        does not implement method lacks field wrong type cannot use mismatched undefined redundant declaration duplicate
        no matching toolchains found for required execution platforms unable to resolve incompatible version unavailable
        command exited with status code invalid argument permission denied socket connection reset refused timed out
        resource exhausted no space left on device killed by signal out of memory checksum mismatch network unreachable
        undeclared inclusion missing strict dependencies could not import compiler failed during analysis of target
        remote local BuildBuddy FAILED_PRECONDITION UNAVAILABLE RESOURCE_EXHAUSTED INTERNAL OOM ENOENT EACCES ENOSPC
        download gcc cc clang ar python3 go GNU realpath ld lld linker tool executable build script failed to start
        :TestRunner GoTestGen GoCompilePkg GoLink GoStdlib test arch arm64 amd64 aarch64 x86_64 linux
        no usable strategy found requires manually registered contains no toolchains unsupported options expected got
        no declaration or dependency with name not visible fetched not enabled not supported parsing main repo mapping
        cannot satisfy visibility discovered not allowed mandatory conflicting exits missing generated outputs
        unrecognized symlink lost sandbox filtered regex typecheck stack affected rule in attribute""".split())
    known.update({w.lower() for w in known})
    save()
    for kind, token in (("tail", None), ("forward", "0")):
        chunks = []
        complete = False
        row = result["console"][kind] = dict(complete=False, pages=0)
        try:
            for _ in range(1 if kind == "tail" else 32):
                payload = {"selector": {"invocation_id": identifier}}
                if token is not None:
                    payload["page_token"] = token
                page = json.loads(fetch("api/v1/GetLog", payload))
                chunk = page.get("log", {}).get("contents", "").encode()
                assert sum(map(len, chunks)) + len(chunk) <= 4 * 1024 * 1024
                chunks.append(chunk)
                token = page.get("next_page_token", page.get("nextPageToken"))
                row["pages"] = len(chunks)
                if not token:
                    complete = True
                    break
        except Exception as error:
            row["lookup_error"] = type(error).__name__
        raw = b"".join(chunks)
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw.decode(errors="replace"))
        projected = []
        matched = 0
        for line in text.splitlines():
            if len(line) > 8192 or re.search(r"(?i)authorization|x-buildbuddy-api-key|--remote_header|--bes_header|command line|credential|bearer|token=|secret", line):
                continue
            if not re.search(r"(?i)error|fatal|fail|undefined|not found|cannot|does not|doesn't|could not|exceed|mismatch|missing|while resolving|no matching|not available|cannot use|during fetch", line):
                continue
            matched += 1
            if len(projected) >= 12:
                continue
            clean = re.sub(r"(?:https?|grpcs?|bytestream)://[^\s]+", " ", line)
            words = re.findall(r"[A-Za-z0-9_+.~-]+", clean)[:110]
            # No fragments of unknown tokens or dynamic URLs are emitted.
            filtered = [s if s in known or s.lower() in known else "…" for s in words]
            filtered = [s for i,s in enumerate(filtered) if s != "…" or not i or filtered[i-1] != "…"]
            locations = []
            for path, ln in re.findall(r"([A-Za-z0-9_./-]+):([0-9]{1,6})(?::[0-9]{1,6})?", clean):
                suffix = next((s for s in ["/".join(path.split("/")[i:]) for i in range(len(path.split("/")))] if s in paths), None)
                if suffix:
                    locations.append([suffix, int(ln)])
            projected.append({"words": filtered[:85], "source": locations[:3], "sha256": hashlib.sha256(line.encode()).hexdigest()})
        row.update(complete=complete, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), matched=matched, diagnostics=projected)
        save()
    result["status"] = "ORIGINAL_LOGS_READ" if any(r.get("bytes") for r in result["console"].values()) else "INCOMPLETE"
    save()
    assert result["status"] == "ORIGINAL_LOGS_READ"

main()
