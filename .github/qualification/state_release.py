"""Compare pooled graph loads and verify a guest read after checkpoint restore."""

import collections
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from xml.etree import ElementTree

work = Path(os.environ["RUNNER_TEMP"]) / "state-release"
records = work / "records"
arms = {"stock": "0e60eabcc2ef88f5962db3f988813faee6a391df",
        "fixed": "370940da75fccafe7b07d01f2edb9b74dc0ce0dd"}
state_test = "//pkg/state:state_test"
tests_test = "//pkg/state/tests:tests_test"
guest_test = "//runsc/container:container_test"


def record(name: str, value: object) -> None:
    (records / (name + ".json")).write_text(json.dumps(value, indent=2) + "\n")


def select(arm: str) -> None:
    subprocess.run(["git", "checkout", "-q", "--detach", arms[arm]], check=True)
    subprocess.run(["git", "diff", "--exit-code", "--quiet"], check=True)


def bazel(verb: str, arguments: list[str], arch: str = "amd64") -> subprocess.CompletedProcess[str]:
    profile = "source-presubmit" if arch == "amd64" else "source-presubmit-arm64"
    command = [str(work / "bazel"), "--bazelrc=" + str(work / "remote.bazelrc"),
               "--host_jvm_args=-Xmx2800m", verb, "--config=" + profile, *arguments]
    p = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in p.stderr.splitlines():
        if ("Invocation ID:" in line or line.startswith("ERROR:")) and not re.search(
                r"remote_header|bes_header|api.key|authorization|cookie", line, re.I):
            print(line[:350], flush=True)
    return p


def binary(label: str, name: str) -> None:
    bazel("build", [label]).check_returncode()
    query = bazel("cquery", ["--output=files", label])
    query.check_returncode()
    file, = [Path(p) for p in query.stdout.splitlines() if Path(p).is_file() and os.access(p, os.X_OK)]
    shutil.copy2(file, work / name)
    with file.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    record(name + "-build", {"target": label, "sha256": digest,
                             "head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()})


def build() -> None:
    binary("@org_golang_x_perf//cmd/benchstat", "benchstat")
    for arm in arms:
        select(arm)
        binary(state_test, arm)


def measure() -> None:
    cpus = os.sched_getaffinity(0)
    os.sched_setaffinity(0, {min(cpus)})
    env = dict(os.environ, GOGC="off", GOMAXPROCS="1", GO_TEST_WRAP="0")
    lines: dict[str, list[str]] = {arm: [] for arm in arms}
    for pair in range(12):
        for arm in (arms if pair % 2 == 0 else reversed(arms)):
            p = subprocess.run([str(work / arm), "-test.run=^$", "-test.bench=^BenchmarkSmallGraphAfterRestore$",
                                "-test.benchtime=128x", "-test.cpu=1"], env=env, text=True, capture_output=True)
            if p.returncode:
                print((p.stdout + p.stderr)[-1800:])
                p.check_returncode()
            rows = [line for line in p.stdout.splitlines() if line.startswith("BenchmarkSmallGraphAfterRestore/")]
            if not (len(rows) == 2 and all("primed-callbacks" in line for line in rows)
                    and {line.split()[0].removesuffix("-1").split("/")[-1] for line in rows} == {"Small", "Large"}
                    and re.search(r"^PASS$", p.stdout, re.M)):
                raise ValueError(rows)
            lines[arm].extend(rows)
        for arm in arms:
            (records / (arm + ".bench")).write_text("\n".join(lines[arm]) + "\n")
    out = subprocess.check_output([work / "benchstat", records / "stock.bench", records / "fixed.bench"], text=True)
    (records / "benchstat.txt").write_text(out)
    print(out)


def test(arm: str, arch: str, owners: list[str], guest: bool) -> bool:
    select(arm)
    name = arm + "-" + arch + ("-guest" if guest else "-state")
    bep = work / (name + ".bep")
    patterns = work / (name + ".targets")
    patterns.write_text("\n".join(owners) + "\n")
    args = ["--target_pattern_file=" + str(patterns), "--build_event_json_file=" + str(bep),
            "--test_env=GO_TEST_WRAP_TESTV=1", "--test_sharding_strategy=disabled",
            "--symlink_prefix=bazel-" + name + "-"]
    if guest:
        args.extend(["--test_tag_filters=", "--test_filter=^TestCheckpointTmpfsTreeForProfile$", "--test_timeout=600"])
    result = bazel("test", args, arch)
    children = {}
    if not bep.exists():
        record(name, {"head": arms[arm], "bazel": result.returncode, "missing": "build event stream"})
        return False
    with bep.open() as stream:
        events = [event for line in stream if "testResult" in line and "testResult" in (event := json.loads(line))]
    for ev in events:
        if "testResult" not in ev or (label := ev["id"]["testResult"]["label"]) not in owners:
            continue
        if label in children:
            raise ValueError(f"Unexpected second result for {label}")
        data = ev["testResult"]
        info = data.get("executionInfo", {})
        output = Path("bazel-" + name + "-testlogs") / label[2:].replace(":", "/")
        digests = {}
        for item in data.get("testActionOutput", []):
            if (filename := item.get("name")) not in {"test.xml", "test.log"}:
                continue
            remote = re.search(r"/(?:blobs|compressed-blobs/zstd)/([0-9a-f]{64})/[0-9]+(?:$|\?)",
                               item.get("uri", ""))
            digest = remote[1] if remote else item.get("digest")
            path = output / filename
            if path.is_file() and digest and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
                digests[filename] = digest
        counts: collections.Counter[str] = collections.Counter()
        names: list[str] = []
        if "test.xml" in digests:
            for case in ElementTree.parse(output / "test.xml").iter("testcase"):
                if case.find("skipped") is not None or case.get("status") in {"notrun", "suppressed"}:
                    counts["skip"] += 1
                elif case.find("error") is not None or case.find("failure") is not None:
                    counts["fail"] += 1
                else:
                    counts["pass"] += 1
                    names.append(case.get("name", "").rsplit(".", 1)[-1])
        log = (output / "test.log").read_text(errors="replace") if "test.log" in digests else ""
        witness = [m.group(0) for m in re.finditer(r"TMPFS-(?:VERIFIED 50000|GRAPH kernel=true|CAP (?:save|restore) 2147483648|NS (?:restore|new-to-guest) [0-9]+)", log)]
        ok = (data["status"] == "PASSED" and info.get("strategy") == "remote"
              and not data.get("cachedLocally") and not info.get("cachedRemotely")
              and counts["pass"] > 0 and not counts["fail"])
        if guest:
            ok = ok and "TestCheckpointTmpfsTreeForProfile" in names and all(
                marker in witness for marker in ("TMPFS-VERIFIED 50000", "TMPFS-GRAPH kernel=true",
                                                  "TMPFS-CAP save 2147483648", "TMPFS-CAP restore 2147483648"))
        else:
            required = ({"TestGraphDependencyCycle", "TestGraphDeferredObjectDecode"}
                        if label == state_test else {"TestTruncatedStreams", "TestSliceOfArraysRoundTrip"})
            ok = ok and required.issubset(names)
        children[label] = {"status": data["status"], "strategy": info.get("strategy"), "counts": counts,
                           "outputs": digests, "witness": witness, "verified": ok}
    record(name, {"head": arms[arm], "bazel": result.returncode, "cases": children})
    return result.returncode == 0 and set(children) == set(owners) and all(v["verified"] for v in children.values())


match sys.argv[1]:
    case "build":
        build()
    case "measure":
        measure()
    case "suites":
        outcomes = [test("fixed", arch, [state_test, tests_test], False) for arch in ("amd64", "arm64")]
        sys.exit(not all(outcomes))
    case "guest":
        outcomes = [test(arm, "amd64", [guest_test], True) for arm in arms]
        sys.exit(not all(outcomes))
    case arg:
        raise ValueError(arg)
