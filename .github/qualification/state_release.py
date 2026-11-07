"""Read per-frame table sizes from the verified native Sentry restore."""

import collections
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from xml.etree import ElementTree

work = Path(os.environ["RUNNER_TEMP"]) / "state-release"
records = work / "records"
source = "9f35c3acc81adce19de9e1330035b6b899bdaf8e"
guest_test = "//runsc/container:container_test"


def record(name: str, value: object) -> None:
    (records / (name + ".json")).write_text(json.dumps(value, indent=2) + "\n")


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


def run(arch: str) -> bool:
    subprocess.run(["git", "checkout", "-q", "--detach", source], check=True)
    subprocess.run(["git", "diff", "--exit-code", "--quiet"], check=True)
    owners = [guest_test]
    name = arch + "-guest"
    bep = work / (name + ".bep")
    patterns = work / (name + ".targets")
    patterns.write_text("\n".join(owners) + "\n")
    args = ["--target_pattern_file=" + str(patterns), "--build_event_json_file=" + str(bep),
            "--test_env=GO_TEST_WRAP_TESTV=1", "--test_sharding_strategy=disabled",
            "--symlink_prefix=bazel-" + name + "-"]
    args.extend(["--test_tag_filters=", "--test_filter=^TestCheckpointTmpfsTreeForProfile$", "--test_timeout=600"])
    result = bazel("test", args, arch)
    children = {}
    if not bep.exists():
        record(name, {"head": source, "bazel": result.returncode, "missing": "build event stream"})
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
        ok = ok and "TestCheckpointTmpfsTreeForProfile" in names and all(
            marker in witness for marker in ("TMPFS-VERIFIED 50000", "TMPFS-GRAPH kernel=true",
                                              "TMPFS-CAP save 2147483648", "TMPFS-CAP restore 2147483648"))
        frame_pattern = (r'FRAME-CENSUS root=(\S+) previous="([^"\s]*)" previousCallbacks=(\d+) '
                         r'callbacks=(\d+) kept=(true|false) live=(\d+) capacity=(\d+)')
        frames = [{"root": root, "previous": previous, "previousCallbacks": int(prior), "callbacks": int(current),
                   "kept": kept == "true", "liveBytes": int(live), "capacityBytes": int(capacity)}
                  for root, previous, prior, current, kept, live, capacity in re.findall(frame_pattern, log)]
        ok = ok and {"*cpuid.FeatureSet", "*kernel.Kernel"}.issubset(frame["root"] for frame in frames)
        children[label] = {"status": data["status"], "strategy": info.get("strategy"), "counts": counts,
                           "outputs": digests, "frames": frames, "witness": witness, "verified": ok}
    record(name, {"head": source, "bazel": result.returncode, "cases": children})
    return result.returncode == 0 and set(children) == set(owners) and all(v["verified"] for v in children.values())


outcomes = [run(arch) for arch in ("amd64", "arm64")]
sys.exit(not all(outcomes))
