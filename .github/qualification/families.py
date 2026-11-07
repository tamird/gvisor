"""Qualify the newly selected ARM64 tests and the binary inputs of build_test."""

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
from urllib.parse import unquote, urlparse

from native_graph import BAZEL, CONTROL, LABEL, SOURCE, SUFFIX, WORK, invoke, save


# Digest of the 21 missing owner labels in artifact 13568767915.
ORIGINAL_MISSING = "b1aaaebca35fa24f4eb1535ca4896aab8d3487d9e840cb1879fc555b38f7c201"
AMD64_CONTROL = "//:buildkite_pipelines_test"
ELF_OWNER = "//test/e2e:integration_test"
ELF_PATH = re.compile(r"bazel-out/[^/]+/bin/test/e2e/(?:[^/]+/)*integration_test")
WORKER = r'''set -eu
machine=$(/bin/uname -m)
case "$TEST_TARGET" in
    *'_presubmit_arm64') test "$machine" = aarch64; field='CPU implementer' ;;
    *) test "$machine" = x86_64; field='vendor_id' ;;
esac
/bin/grep -Eq "^$field[[:space:]]*:" /proc/cpuinfo
printf 'PRESUBMIT_NATIVE %s %s\n' "$TEST_TARGET" "$machine"
exec "$@"'''
CASES = [
    "action conflict", "no matching toolchains", "no matching execution platform",
    "no registered executors", "exec format error", "resource_exhausted", "permission denied",
    "no such file or directory", "out of memory", "deadline exceeded", "no space left",
    "error loading", "denied", "failed to build", "run_under",
]
TEST_OPTIONS = [
    "--nocache_test_results", "--flaky_test_attempts=1", "--modify_execution_info=^TestRunner$=+no-remote-cache",
    "--run_under=" + shlex.join(["/bin/sh", "-c", WORKER, "native-probe"]),
]


def read(name: str) -> dict:
    return json.loads((WORK / "results" / (name + ".json")).read_text())


def preflight():
    # The public build_test uses genrule srcs to force its original test binaries to build.
    labels = subprocess.check_output([
        *BAZEL, "query", "--output=label",
        'labels(srcs,kind("^genrule rule$",deps(//:build_test,1)))',
    ], text=True).splitlines()
    assert len(set(labels)) == len(labels) == 16 and ELF_OWNER in labels
    assert all(LABEL.fullmatch(label) for label in labels)
    star = WORK / "binaries.star"
    star.write_text("wanted = " + repr(sorted(labels)) + "\n" + '''
def format(target):
    label = str(target.label).lstrip("@")
    if label not in wanted:
        return ""
    opts = build_options(target)
    if opts.get("//command_line_option:is exec configuration", False):
        return ""
    return "\\t".join([label,str(opts["//command_line_option:cpu"]),
                       str(opts["//command_line_option:platforms"]),"|".join([f.path for f in target.files.to_list()])])
''')
    output, _, code = invoke("mixed", "binaries", "cquery", [
        *TEST_OPTIONS, "--output=starlark", "--starlark:file=" + str(star), "deps(//:build_test" + SUFFIX + ")",
    ])
    assert code == 0, code
    inputs = {}
    for line in output.read_text().splitlines():
        if line:
            label, cpu, platform, file = line.split("\t")
            assert cpu == "aarch64" and "linux_arm64" in platform, (label, cpu, platform)
            assert file.startswith("bazel-out/") and "|" not in file, (label, file)
            assert inputs.setdefault(label, file) == file, label
    assert inputs.keys() == set(labels) and ELF_PATH.fullmatch(inputs[ELF_OWNER]), inputs
    save("build-inputs", {"source": SOURCE, "arm64_files": inputs})


def targets() -> dict[str, str]:
    assert read("comparison")["qualified"]
    native = read("arm64-actions")["shards"]
    names = sorted(r["owner"] for r in native if r["rule"] != "go_test rule" and r["owner"] != CONTROL)
    digest = hashlib.sha256(("\n".join(names) + "\n").encode()).hexdigest()
    assert len(set(names)) == len(names) == 21 and digest == ORIGINAL_MISSING
    return {AMD64_CONTROL: "x86_64"} | {name + SUFFIX: "aarch64" for name in names}


def execute():
    selected = targets()
    patterns = WORK / "new-arm64.targets"
    patterns.write_text("\n".join(selected) + "\n")
    with (WORK / "physical.log").open("w") as log:
        rc = subprocess.run([
            *BAZEL, "--bazelrc=" + str(WORK / "remote.rc"), "test", "--config=source-presubmit-mixed",
            "--target_pattern_file=" + str(patterns), "--test_output=errors", "--color=no", "--curses=no",
            "--jobs=48", "--noremote_local_fallback", *TEST_OPTIONS,
            "--remote_download_outputs=minimal",
            "--remote_download_regex=(.*testlogs/.*/test[.]log|" + ELF_PATH.pattern + ")$",
            "--build_event_json_file=" + str(WORK / "physical.bep"),
        ], stdout=log, stderr=log).returncode
    save("physical-exit", {"source": SOURCE, "exit": rc, "selected": sorted(selected)})


def test_log(label: str, result: dict) -> tuple[bytes, str]:
    outputs = [o for o in result.get("testActionOutput", []) if o["name"] == "test.log"]
    if len(outputs) != 1:
        return b"", ""
    output, = outputs
    uri = urlparse(output.get("uri", ""))
    cas = re.search(r"/(?:blobs|compressed-blobs/zstd)/([0-9a-f]{64})/(\d+)$", uri.path)
    digest = cas[1] if cas else output.get("digest")
    paths = [Path(unquote(uri.path))] if uri.scheme == "file" else []
    sub = label[2:].replace(":", "/").lstrip("/")
    paths.extend(Path("bazel-out").glob("*/testlogs/" + sub + "/test.log"))
    for path in paths:
        if path.is_file() and path.stat().st_size < 8 * 1024 * 1024:
            raw = path.read_bytes()
            actual = hashlib.sha256(raw).hexdigest()
            if not digest or actual == digest:
                return raw, actual
    return b"", ""


def collect():
    selected = targets()
    results = {}
    errors = []
    invocation = None
    complete = True
    for line in (WORK / "physical.bep").open():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            complete = False
            break
        if start := e.get("started"):
            invocation = start["uuid"]
        if (a := e.get("action")) and not a.get("success") and len(errors) < 20:
            owner = e["id"]["actionCompleted"].get("label", "")
            errors.append({"owner": owner if LABEL.fullmatch(owner) else "dependency", "mnemonic": a.get("type")})
        if t := e.get("testResult"):
            label = e["id"]["testResult"]["label"]
            if label not in selected or label in results:
                complete = False
                continue
            raw, digest = test_log(label, t)
            witnesses = re.findall(rb"(?m)^PRESUBMIT_NATIVE (@*//[^\s]+) (x86_64|aarch64)\r?$", raw)
            info = t.get("executionInfo", {})
            results[label] = {"status": t.get("status"), "strategy": info.get("strategy"),
                              "cached": bool(t.get("cachedLocally") or info.get("cachedRemotely")),
                              "worker": [(l.decode().lstrip("@"), m.decode()) for l, m in witnesses],
                              "log_sha256": digest}
    paths = sorted(path for path in Path("bazel-out").glob("*/bin/test/e2e/**/integration_test")
                   if ELF_PATH.fullmatch(str(path)) and path.is_file())
    binaries = []
    for path in paths:
        with path.open("rb") as stream:
            header = stream.read(64)
        binaries.append({"path": str(path), "size": path.stat().st_size, "elf64le": header[:6] == b"\x7fELF\x02\x01",
                         "machine": int.from_bytes(header[18:20], "little")})
    code = read("physical-exit")["exit"] if (WORK / "results/physical-exit.json").exists() else -1
    log = (WORK / "physical.log").read_text(errors="replace").lower()
    expected_path = read("build-inputs")["arm64_files"][ELF_OWNER]
    good = (complete and code == 0 and results.keys() == selected.keys() and all(
        row["status"] == "PASSED" and row["strategy"] == "remote" and not row["cached"]
        and row["worker"] == [(label, selected[label])] for label, row in results.items())
        and any(b["path"] == expected_path and b["elf64le"] and b["machine"] == 183 and b["size"] > 64 for b in binaries))
    save("physical", {"source": SOURCE, "invocation": invocation, "exit": code, "qualified": good,
                      "tests": results, "integration_test_binaries": binaries, "errors": errors,
                      "signals": sorted(term for term in CASES if term in log)})
    print("Native test results", Counter(row["status"] for row in results.values()), "ELF files", len(binaries))
    assert good, "incomplete physical native qualification"


if __name__ == "__main__":
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    {"preflight": preflight, "execute": execute, "collect": collect}[sys.argv[1]]()
