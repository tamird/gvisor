"""Qualify the final standalone checkpoint identity exactly once per target."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

tmp = Path(os.environ["RUNNER_TEMP"])
proof = tmp / "owner-identity-proof"
proof.mkdir(exist_ok=True)
source = os.environ["SOURCE"]
stage = sys.argv[1]
arch = sys.argv[2] if len(sys.argv) > 2 else ""
key = stage + ("-" + arch if arch else "")
record = {"source": source, "stage": key, "result": "FAIL"}
def persist():
    record["recorded"] = datetime.now(timezone.utc).isoformat()
    (proof / (key + ".json")).write_text(json.dumps(record, indent=2) + "\n")
def run(argv, **kwargs):
    return subprocess.run([str(x) for x in argv], check=True, **kwargs)
guest = ["docker", "exec", "-e", "GIT_CONFIG_COUNT=1", "-e", "GIT_CONFIG_KEY_0=safe.directory",
         "-e", "GIT_CONFIG_VALUE_0=" + os.environ["GITHUB_WORKSPACE"], "overlay-guest"]
sentry = "//runsc/cmd/sentry:gvisor_sentry"
ctest = "//runsc/container:container_test"
owners = ["//runsc/boot:boot_nogo", "//runsc/container:container_test_nogo"]
def bazel(operation, labels, extra=()):
    bep = tmp / ("owner-" + key + ".bep")
    log = tmp / ("owner-" + key + ".log")
    common = ["--config=" + {"amd64":"x86_64","arm64":"aarch64"}[arch],
              "--platforms=//tools/bazeldefs:linux_" + arch, "-c", "opt", "--jobs=4",
              "--strategy=GoStaticAnalysis=linux-sandbox", "--strategy=GoStandardLibraryAnalysis=linux-sandbox",
              "--strategy=GoStdlib=linux-sandbox", "--strategy=GoStdlibList=linux-sandbox",
              "--modify_execution_info=^Go(StandardLibrary|Static)Analysis$=-no-sandbox,^Go(Stdlib(List)?|StandardLibraryAnalysis)$=+no-remote-cache",
              "--remote_download_outputs=toplevel", "--stamp","--workspace_status_command=",
              "--build_event_json_file=" + str(bep)]
    argv = [*guest, "bazel", "--bazelrc=" + str(tmp/"overlay.bazelrc"), operation,
            *common, *extra, *labels]
    with log.open("wb") as dst:
        code = subprocess.run(argv, stdout=dst, stderr=subprocess.STDOUT).returncode
    text = log.read_text(errors="replace")
    record.update(labels=labels, exit=code, invocations=sorted(set(re.findall(r"https://app.buildbuddy.io/invocation/[0-9a-f-]+", text))))
    evs = (json.loads(line) for line in bep.open()) if bep.is_file() else ()
    results, completed, refs = {}, [], []
    for e in evs:
        eid = e.get("id",{})
        if "testSummary" in eid:
            results[eid["testSummary"]["label"]] = e["testSummary"]["overallStatus"]
        if "targetCompleted" in eid and e.get("completed",{}).get("success"):
            completed.append(eid["targetCompleted"]["label"])
        if "namedSetOfFiles" in e or "targetCompleted" in eid:
            refs.append(e)
    record.update(summaries=results,completed=sorted(set(completed)))
    (proof / (key + "-output-refs.json")).write_text(json.dumps(refs))
    if code:
        msg = re.sub(r"(?i)(x-buildbuddy-api-key[=: ]+)\S+", r"\1[redacted]", text[-8000:])
        (proof/(key+"-failure.txt")).write_text(msg)
    persist()
    if code or set(labels) - set(completed):
        raise RuntimeError((key, code, completed))
    if operation == "test" and any(results.get(label) != "PASSED" for label in labels):
        raise RuntimeError((key, results))
def check_build():
    labels = [sentry]
    if arch == "amd64":
        labels += ["//:release", "//test/cmd/test_app:test_app"]
    bazel("build", labels)
    exe = tmp / ("owner-"+arch+".sentry")
    run([*guest,"cp","-L","bazel-bin/runsc/cmd/sentry/gvisor_sentry_/gvisor_sentry",exe])
    hdr = subprocess.check_output(["readelf","-h",exe],text=True)
    assert ("AArch64" if arch=="arm64" else "X86-64") in hdr,hdr
    from owner_inspect_elf import notes
    go_id, gnu_id = notes(exe)
    assert go_id and go_id != "redacted",go_id
    with exe.open("rb") as f:
        record.update(machine=arch,go_id=go_id,gnu_id=gnu_id,sha256=hashlib.file_digest(f,"sha256").hexdigest())
    if arch == "amd64":
        run([*guest,"cp","-rL","bazel-bin/release",tmp/"owner-identity-release"])
        run([*guest,"cp","-L","bazel-bin/test/cmd/test_app/test_app_/test_app",tmp/"owner-identity-guest"])
        release_exe=tmp/"owner-identity-release/gvisor-bin/gvisor_sentry"
        with release_exe.open("rb") as f:
            assert hashlib.file_digest(f,"sha256").hexdigest() == record["sha256"]
        version = subprocess.check_output([exe,"--version"],text=True).splitlines()[0].strip()
        assert version in {"runsc version","runsc version VERSION_MISSING"},version
        record["version"] = version
def check_metadata():
    try:
        bazel("test",[ctest],["--strategy=TestRunner=local","--test_sharding_strategy=disabled",
              "--test_filter=^TestCheckpointMetadata$/^systrap$","--test_arg=-test.v","--test_arg=--test_platforms=systrap",
              "--test_output=errors","--nocache_test_results"])
    finally:
        t = subprocess.run([*guest,"cat","bazel-testlogs/runsc/container/container_test/test.log"],text=True,capture_output=True).stdout
        lines = [line for line in t.splitlines() if re.search(r'(=== RUN|--- (SKIP|FAIL|PASS):).*TestCheckpointMetadata',line)]
        record["case_output"] = lines
    assert any("--- PASS: TestCheckpointMetadata/systrap " in line for line in lines),lines
def main():
    if stage == "build":
        check_build()
    elif stage == "nogo":
        bazel("test",owners,["--test_output=errors"])
    elif stage == "metadata":
        check_metadata()
    else:
        raise ValueError(key)
try:
    main()
    record["result"] = "PASS"
except BaseException as exc:
    record["error"] = repr(exc)
    raise
finally:
    persist()
    print(json.dumps(record,indent=2),flush=True)
