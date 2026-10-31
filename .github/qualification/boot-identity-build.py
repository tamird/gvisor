"""Qualify the final standalone checkpoint identity exactly once per target."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def wait_bazel(command, log, destination):
    import time
    started = time.monotonic()
    with log.open("wb") as dst:
        proc = subprocess.Popen(command, stdout=dst, stderr=subprocess.STDOUT)
        while True:
            try:
                code = proc.wait(timeout=45)
                break
            except subprocess.TimeoutExpired:
                with log.open("rb") as stream:
                    stream.seek(max(0, log.stat().st_size-18000))
                    tail=stream.read().decode(errors="replace")
                lines=[s for s in tail.splitlines() if re.search(r"Invocation ID:|https://app.buildbuddy.io/invocation/[a-f0-9-]+|^\[[0-9 ,/]+\]|^Analyzing:|^Loading:",s)]
                safe=re.sub(r"(?i)(x-buildbuddy-api-key[=: ]+)\S+",r"\1[redacted]",lines[-1] if lines else "Bazel active")
                print(destination, "seconds", round(time.monotonic()-started), safe[:360], flush=True)
    return code

tmp = Path(os.environ["RUNNER_TEMP"])
proof = tmp / "boot-identity-results"
proof.mkdir(exist_ok=True)
source = os.environ["SOURCE"]
stage = sys.argv[1]
arch = sys.argv[2] if len(sys.argv) > 2 else ""
arm = os.environ["ARM"]
key = arm + "-build"
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
    argv = [*guest, "timeout","--kill-after=15s","23m","bazel", "--bazelrc=" + str(tmp/"overlay.bazelrc"), operation,
            *common, *extra, *labels]
    print("Bazel optimized release", source, labels, flush=True)
    code = wait_bazel(argv, log, key)
    text = log.read_text(errors="replace")
    record.update(labels=labels, exit=code, invocations=sorted(set(re.findall(r"https://app.buildbuddy.io/invocation/[0-9a-f-]+", text))))
    record["process_summary"]=[line for line in text.splitlines() if re.match(r"INFO: [0-9]+ processes:",line)][-1:]
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
    if code:
        msg = re.sub(r"(?i)(x-buildbuddy-api-key[=: ]+)\S+", r"\1[redacted]", text[-8000:])
        (proof/(key+"-failure.txt")).write_text(msg)
    persist()
    if code or set(labels) - set(completed):
        raise RuntimeError((key, code, completed))
    if operation == "test" and any(results.get(label) != "PASSED" for label in labels):
        raise RuntimeError((key, results))

try:
    labels = ['//:release', '//test/rbe/boot_identity:guest']
    bazel('build', labels)
    run([*guest,'cp','-rL','bazel-bin/release',tmp/('install-'+arm)])
    run([*guest,'cp','-L','bazel-bin/test/rbe/boot_identity/guest_/guest',tmp/('probe-'+arm)])
    record['result']='PASS'
finally:
    persist()
