"""Verify retained RACK test reports without running Bazel again."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree


ORIGINAL = {
    "joint": ("41010892540", "699298e6761f645cc469aaf106e25e143232f919", ("joint",)),
    "primary": ("41009307713", "b2d2cb2fdf4f48f37a8ad984f2e5d41c69c65262", ("control", "tests", "analyze", "race")),
}
MAX_BLOB = 128 << 20
MAX_SUM = 512 << 20


def record(contents):
    return {"bytes": len(contents), "sha256": hashlib.sha256(contents).hexdigest()}


def write_json(path, payload):
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def resolve(uri, name, cache):
    parsed = urlsplit(uri)
    match = re.fullmatch(r"/blobs/([a-f0-9]{64})/(\d+)", parsed.path)
    if (not match or parsed.scheme != "bytestream" or parsed.netloc != "remote.buildbuddy.io"
            or parsed.query or parsed.fragment):
        raise ValueError("unexpected test report URI")
    digest, length = match.groups()
    size = int(length)
    if size > MAX_BLOB:
        raise ValueError(f"report exceeds recovery limit: {size}")
    dest = cache / digest / name
    dest.parent.mkdir(exist_ok=True)
    if not dest.exists():
        req = Request("https://app.buildbuddy.io/api/v1/GetFile", json.dumps({"uri": uri}).encode(), {
            "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"],
            "Content-Type": "application/json",
        })
        try:
            with urlopen(req, timeout=60) as response:
                data = response.read(size + 1)
        except HTTPError as e:
            raise ValueError(f"GetFile rejected {digest}/{size} with HTTP {e.code}") from None
        if record(data) != {"sha256": digest, "bytes": size}:
            raise ValueError(f"GetFile digest/length mismatch for {digest}/{size}")
        dest.write_bytes(data)
    elif record(dest.read_bytes()) != {"sha256": digest, "bytes": size}:
        raise ValueError("local cached report mismatched advertised digest")
    return dest, {"sha256": digest, "bytes": size}


def main():
    mode, artifact, root = sys.argv[1:]
    run, sha, arms = ORIGINAL[mode]
    original = Path(artifact)
    root = Path(root)
    work = Path(os.environ["RUNNER_TEMP"]) / f"recovered-{mode}"
    work.mkdir()
    cache = work / "cas"
    cache.mkdir()
    report = work / "qualification_report.py"
    checker = work / "rack_ack_result.py"
    for script in (report, checker, work / "unit_matrix.py"):
        blob = subprocess.check_output(["git", "show", f"{sha}:test/rbe/{script.name}"])
        script.write_bytes(blob)
    summary = {"original_run": run, "qualification_source": sha, "arms": {}}
    errors = []
    for arm in arms:
        try:
            orig = original / arm
            if not (orig / "events.jsonl").exists():
                raise ValueError("original arm emitted no retained BEP")
            out = work / arm
            out.mkdir()
            event_data = (orig / "events.jsonl").read_bytes()
            manifest = json.loads((orig / "events.jsonl.manifest.json").read_bytes())
            if record(event_data) != manifest["events"]:
                raise ValueError("retained original BEP does not match its manifest")
            for name in ("commit", "head", "context.json", "arguments", "before", "after", "exit", "finish", "targets"):
                shutil.copyfile(orig / name, out / name)
            refs = []
            total = 0
            # Preserve all event fields and replace only the two report transports.
            projected = out / "reports-local.jsonl"
            with projected.open("w") as dest:
                for line in event_data.splitlines():
                    event = json.loads(line)
                    for item in event.get("testResult", {}).get("testActionOutput", ()):
                        if item.get("name") not in ("test.log", "test.xml"):
                            continue
                        source, rec = resolve(item["uri"], item["name"], cache)
                        total += rec["bytes"]
                        if total > MAX_SUM:
                            raise ValueError("recovery size limit exceeded")
                        refs.append({"identity": event["id"]["testResult"], "name": item["name"], **rec})
                        item["uri"] = source.as_uri()
                    dest.write(json.dumps(event) + "\n")
            cap = subprocess.run([sys.executable, "-B", report, "capture", "--events", projected,
                                  "--output", out / "events.jsonl", "--output-root", cache,
                                  "--report-dir", out / "original"], capture_output=True)
            (out / "capture-exit").write_text(f"{cap.returncode}\n")
            proc = subprocess.run([sys.executable, "-B", report, "verdict", "--events", out / "events.jsonl",
                                   "--context", out / "context.json", "--expected-targets", out / "targets",
                                   "--output", out / "verdict.json"], capture_output=True)
            v = json.loads((out / "verdict.json").read_text())
            selected = []
            for log in (out / "original").rglob("test.log"):
                for disposition, case in re.findall(r"--- (PASS|FAIL|SKIP): (TestRACK[^\s]+)", log.read_text(errors="replace")):
                    selected.append([disposition, case])
            xml_cases = []
            for xml in (out / "original").rglob("test.xml"):
                for case in ElementTree.parse(xml).getroot().iter("testcase"):
                    name = case.attrib.get("name", "")
                    if name.startswith("TestRACK"):
                        status = "FAIL" if case.find("failure") is not None or case.find("error") is not None else "SKIP" if case.find("skipped") is not None else "PASS"
                        xml_cases.append([status, name])
            summary["arms"][arm] = {"original_receipt": manifest["events"], "original_capture_exit": int((orig / "capture-exit").read_text()),
                                    "resolved": refs, "recovery_capture_exit": cap.returncode, "verifier_exit": proc.returncode,
                                    "selected_cases": selected, "xml_cases": xml_cases,
                                    **{k: v[k] for k in ("source", "status", "native_exit", "executed", "missing", "skips", "case_counts",
                                                         "execution_finished_at", "verdict_available_at","errors")}}
        except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as ex:
            errors.append(f"{arm}: {ex}")
    final = subprocess.run([sys.executable, "-B", checker, "final", work], capture_output=True)
    if (work / "verdict.json").exists():
        summary["original_case_verifier"] = json.loads((work / "verdict.json").read_text())
    summary["case_verifier_exit"] = final.returncode
    summary["recovery_errors"] = errors
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / f"{mode}.json", summary)
    for arm, result in summary["arms"].items():
        print(arm, result["source"], result["native_exit"], result["case_counts"], result["missing"], result["selected_cases"])
    print("original case verifier exit", final.returncode, "recovery errors", errors)
    return bool(errors or final.returncode)


if __name__ == "__main__":
    sys.exit(main())
