"""Check the original checker and generated-package analysis results."""

import hashlib
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import qualification_report as reporting

OWNERS = ("//tools/nogo/check:check_test_nogo", "//pkg/eventchannel:eventchannel_nogo", "//pkg/sync:sync_nogo")
ORIGINAL = "e0633e9a-5007-4df7-b5be-739e8524afa5"


def native():
    out, source, destination, phase = sys.argv[2:]
    out = Path(out)
    events = out / "events.jsonl"
    captured = reporting.capture(out / "raw.jsonl", events, Path("bazel-out"), out / "reports")
    (out / "capture_exit").write_text(str(int(bool(captured["errors"]))))
    context = {key: key for key in ("commit_file", "source_before", "source_after", "final_head",
                                     "executed_arguments", "native_exit", "capture_exit")}
    context.update(expected_source=source, command_kind="test",
                   execution_finished_at=(out / "finished").read_text().strip())
    reporting.write_json(out / "context.json", context)
    verdict = reporting.verdict(events, reporting.manifest_path(events), out / "context.json",
                                out / "targets.txt", ())
    targets = set((out / "targets.txt").read_text().splitlines())
    cases = {}
    for row in captured["reports"]:
        if row["name"] != "test.xml":
            continue
        label = row["identity"]["label"]
        xml = ET.parse(out / row["path"])
        actual = [f"{c.get('classname')}.{c.get('name')}" for c in xml.iter("testcase")]
        if not actual or label in cases:
            verdict["errors"].append(f"{label}: missing or repeated named results")
        cases[label] = actual
    attempts = []
    for line in events.read_text().splitlines():
        event = json.loads(line)
        if "started" in event:
            verdict["invocation"] = event["started"]["uuid"]
        if "testResult" not in event:
            continue
        result = event["testResult"]
        label = event["id"]["testResult"]["label"]
        attempts.append(label)
        info = result.get("executionInfo", {})
        if result.get("cachedLocally") or info.get("cachedRemotely") or info.get("strategy") != "remote":
            verdict["errors"].append(f"{label}: expected uncached remote execution")
    required_targets = set(OWNERS) if phase == "public" else {"//tools/nogo/qualification_legacy:legacy_test"}
    if targets != required_targets or set(cases) != targets or sorted(attempts) != sorted(targets):
        verdict["errors"].append("missing or duplicate result or XML")
    verdict["cases_by_target"] = cases
    verdict["runner_exit"] = int((out / "runner_exit").read_text())
    if verdict["status"] == "PASS" and (verdict["runner_exit"] or verdict["errors"] or verdict["skips"]):
        verdict["status"] = "FAIL"
    reporting.write_json(Path(destination), verdict)
    print({k: verdict.get(k) for k in ("status", "errors", "missing", "cases_by_target", "invocation")})
    return int(verdict["status"] != "PASS")


def verify_actions():
    phase, destination, *names = sys.argv[2:]
    paths = [Path(name) for name in names]
    verdict = json.loads(Path(destination).read_text())
    assert verdict["status"] == "PASS", "native tests must pass first"
    if phase == "public":
        rendered = []
        for actions_path in paths:
            for action in json.loads(actions_path.read_text()).get("actions", []):
                args = action.get("arguments", [])
                if "render" in args and any(arg.startswith("-output=") and arg.endswith("/runtime_constants_go_impl.go") for arg in args):
                    rendered.extend(arg.removeprefix("-GOARCH=") for arg in args if arg.startswith("-GOARCH="))
        verdict["sync_constants_render"] = sorted(rendered)
        if sorted(set(rendered)) != ["amd64", "arm64"]:
            verdict["errors"].append("missing //pkg/sync:runtime_constants_go_render action for amd64 or arm64")
    elif phase == "legacy":
        observed = []
        for actions_path in paths:
            actions = json.loads(actions_path.read_text()).get("actions", [])
            prefix = "gvisor.dev/gvisor/tools/nogo/qualification_legacy/"
            for action in actions:
                args = action.get("arguments", [])
                if f"-package={prefix}consumer" not in args:
                    continue
                inputs = {arg.removeprefix("-compiler-import=").split("=", 1)[0]:
                          Path(arg.rsplit("=", 1)[-1]).suffix
                          for arg in args if arg.startswith("-compiler-import=")}
                arch, = [arg.removeprefix("-GOARCH=") for arg in args if arg.startswith("-GOARCH=")]
                observed.append({"arch": arch, "inputs": inputs})
        verdict["archive_only_actions"] = observed
        if sorted(r["arch"] for r in observed) != ["amd64", "arm64"] or any(
            r["inputs"] != {prefix + "own": ".x", prefix + "extra": ".x"} for r in observed
        ):
            verdict["errors"].append("missing own/hook archive inputs for both consumer configurations")
    else:
        raise ValueError(phase)
    verdict["status"] = "FAIL" if verdict["errors"] else "PASS"
    reporting.write_json(Path(destination), verdict)
    print({key: verdict.get(key) for key in ("status", "errors", "sync_constants_render")})
    return int(verdict["status"] != "PASS")


def original():
    from cas_inputs import fetch

    dest = Path(sys.argv[2])
    log = Path(sys.argv[3]).read_text()
    invocation = log.index("INFO: Invocation ID: " + ORIGINAL)
    stop = log.index("INFO: Invocation ID:", invocation + 24)
    block = log[invocation:stop]
    finished, = re.findall(r"(2026-[0-9T:.-]+Z) INFO: Build completed successfully,", block)
    assert "Executed 3 out of 3 tests: 3 tests pass" in block
    assert all(re.search(re.escape(owner) + r" +PASSED in ", block) for owner in OWNERS)

    def read(method, **selector):
        data = json.loads(fetch("api/v1/Get" + method, {"selector": {"invocation_id": ORIGINAL, **selector}}))
        assert not data.get("nextPageToken", data.get("next_page_token")), "Unexpected page"
        return data[method.lower()]

    metadata, = read("Invocation")
    assert metadata["id"].get("invocationId", metadata["id"].get("invocation_id")) == ORIGINAL
    assert metadata["command"] == "test" and metadata["success"]
    source = metadata.get("commitSha", metadata.get("commit_sha"))
    # The frozen original workflow also gated this exact checkout before running Bazel.
    if source:
        assert source == os.environ["SOURCE"], "Bazel metadata disagrees with original checkout"
    assert {t["label"]:t["status"] for t in read("Target")} == dict.fromkeys(OWNERS, "PASSED")
    out = {"source": os.environ["SOURCE"], "bazel_source": source, "invocation": ORIGINAL,
           "native_exit": 0, "errors": [], "missing": [], "skips": [], "targets": {},
           "status": "INCOMPLETE", "execution_finished_at": finished}
    reporting.write_json(dest, out)
    for owner in OWNERS:
        matches = [a for a in read("Action", target_label=owner)
                   if any(f["name"] == "test.xml" for f in a.get("file", []))]
        action, = matches
        assert action.get("targetLabel", action.get("target_label")) == owner
        assert action["id"].get("invocationId", action["id"].get("invocation_id")) == ORIGINAL
        assert all(int(action[k]) == 1 for k in ("run", "shard", "attempt"))
        row = {}
        for name in ("test.xml", "test.log"):
            file, = [f for f in action["file"] if f["name"] == name]
            match = re.fullmatch(r"bytestream://remote[.]buildbuddy[.]io/(?:[^?#]*/)?blobs/([a-f0-9]{64})/([0-9]+)", file["uri"])
            assert match and 0 < int(match[2]) < 1024 * 1024
            data = fetch("file/download?" + urlencode({"bytestream_url":file["uri"], "invocation_id":ORIGINAL}))
            assert len(data) == int(match[2]) and hashlib.sha256(data).hexdigest() == match[1]
            row[name] = match[1]
            if name == "test.xml":
                tree = ET.fromstring(data)
                case, = tree.iter("testcase")
                assert case.get("name") == owner.removeprefix("//").replace(":", "/")
                assert not any(list(tree.iter(tag)) for tag in ("failure", "error", "skipped"))
                assert case.get("status") != "notrun" and case.get("result") != "suppressed"
                row["case"] = case.get("name")
            else:
                row["architectures"] = [arch for arch in ("amd64", "arm64") if arch.encode()+b"..." in data]
                assert len(row["architectures"]) == 2, "one architecture missing from original test.log"
        out["targets"][owner] = row
        reporting.write_json(dest, out)
    out["status"] = "PASS"
    out["recovered_at"] = datetime.now(timezone.utc).isoformat()
    reporting.write_json(dest, out)
    return 0


if __name__ == "__main__":
    sys.exit({"native": native, "original": original, "actions": verify_actions}[sys.argv[1]]())
