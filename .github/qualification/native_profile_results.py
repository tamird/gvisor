"""Read both native presubmit results and compare their shards to the previous full run."""
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
import xml.etree.ElementTree as ET

WORK = Path(os.environ["RUNNER_TEMP"]) / "ordinary-profiles"
PERMISSION = "//runsc/cmd:statefile_output_permissions_test"
CMD = "//runsc/cmd:cmd_test"
CASES = {"readonly_existing_file_rejected_when_unprivileged",
         "dirs_without_read_perm_succeed_when_unprivileged"}
STATUSES = {"NO_STATUS", "PASSED", "FAILED", "FLAKY", "TIMEOUT", "INCOMPLETE",
            "REMOTE_FAILURE", "TOOL_HALTED_BEFORE_TESTING", "FAILED_TO_BUILD"}


def named_results(arch, label, result):
    item = next((f for f in result.get("testActionOutput", []) if f.get("name") == "test.xml"), {})
    remote = re.search(r"/(?:blobs|compressed-blobs/zstd)/([0-9a-f]{64})/\d+(?:$|\?)", item.get("uri", ""))
    digest = remote[1] if remote else item.get("digest")
    path = Path(f"bazel-qual-{arch}-testlogs/runsc/cmd", label.split(":")[1], "test.xml")
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        return {}
    names = {}
    counts = collections.Counter()
    for case in ET.parse(path).iter("testcase"):
        name = case.get("name", "")
        status = "PASS"
        if case.find("skipped") is not None or case.get("status") == "notrun" or case.get("result") in ("skipped", "suppressed"):
            status = "SKIP"
        if case.find("failure") is not None or case.find("error") is not None:
            status = "FAIL"
        counts[status] += 1
        if label == PERMISSION and name.split("/")[-1] in CASES:
            names[name.split("/")[-1]] = status
        if label == CMD and "osture" in name and re.fullmatch(r"[A-Za-z0-9_/.-]{1,240}", name):
            names[name] = status
    return {"digest": digest, "counts": counts, "names": names}


def collect(arch, expected):
    seen, summaries, built = set(), set(), set()
    statuses, aborts = collections.Counter(), collections.Counter()
    failures, xml = {}, {}
    cached = other_strategy = duplicates = 0
    invocation = code = None
    bep = WORK / f"{arch}.bep"
    if bep.exists():
        with bep.open() as records:
            for raw in records:
                event = json.loads(raw)
                ident = event["id"]
                if "started" in event:
                    invocation = str(uuid.UUID(event["started"]["uuid"]))
                if "finished" in event:
                    code = int(event["finished"]["exitCode"].get("code", 0))
                if "targetCompleted" in ident and event.get("completed", {}).get("success"):
                    built.add(ident["targetCompleted"]["label"])
                if "aborted" in event:
                    reason = event["aborted"].get("reason", "UNKNOWN")
                    aborts[reason if re.fullmatch(r"[A-Z_]{1,64}", reason) else "UNKNOWN"] += 1
                if "testSummary" in event:
                    label = ident["testSummary"]["label"]
                    assert re.fullmatch(r"//[A-Za-z0-9_./+-]*:[A-Za-z0-9_./+-]+", label)
                    summaries.add(label)
                if "testResult" not in event:
                    continue
                label = ident["testResult"]["label"]
                assert re.fullmatch(r"//[A-Za-z0-9_./+-]*:[A-Za-z0-9_./+-]+", label)
                key = (label, int(ident["testResult"].get("shard", 1)))
                duplicates += key in seen
                seen.add(key)
                result = event["testResult"]
                info = result.get("executionInfo", {})
                cached += bool(result.get("cachedLocally") or info.get("cachedRemotely"))
                other_strategy += info.get("strategy") != "remote"
                status = result.get("status", "NO_STATUS")
                assert status in STATUSES
                statuses[status] += 1
                if status != "PASSED":
                    failures[str(key)] = status
                if label in (PERMISSION, CMD):
                    xml[label] = named_results(arch, label, result)
    owners = {owner for owner, _ in expected}
    permission = xml.get(PERMISSION, {}).get("names") == dict.fromkeys(CASES, "PASS")
    return dict(invocation=invocation, bazel_exit=code, expected_shards=len(expected),
                received_shards=len(seen), statuses=statuses, failures=failures, cached=cached,
                non_remote=other_strategy, duplicates=duplicates, missing=sorted(expected-seen),
                extra=sorted(seen-expected), missing_summaries=sorted(owners-summaries),
                extra_summaries=sorted(summaries-owners), successful_build_targets=len(built),
                aborts=aborts, named_xml=xml, complete_shards=seen==expected and not duplicates,
                passed=bool(code==0 and not failures and permission and xml.get(CMD)
                            and seen==expected and summaries==owners and not (duplicates or cached or other_strategy)))


if __name__ == "__main__":
    oracle = {label:int(count) for label,count in
              (line.split() for line in (WORK/"oracle.tsv").read_text().splitlines())}
    assert len(oracle)==364 and sum(oracle.values())==374
    expected = {(owner, n) for owner, count in oracle.items() for n in range(1, count+1)}
    profiles = {arch:collect(arch, expected) for arch in ("amd64", "arm64")}
    step_exit = int((WORK/"step.exit").read_text()) if (WORK/"step.exit").is_file() else None
    result = {"source":"4eb27c4081e535d69c1257998a6c94256d9812e2", "step_exit":step_exit,
              "profiles":profiles, "complete_pass":step_exit==0 and all(p["passed"] for p in profiles.values())}
    (WORK/"receipt.json").write_text(json.dumps(result, indent=2, sort_keys=True)+"\n")
