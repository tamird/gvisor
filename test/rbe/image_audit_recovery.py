"""Recover exact missing LTP producers from authenticated remote audit files."""

import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import time
import urllib.request

from image_execution import execution_records

# The owning driver supplies its already authenticated reporter directory.
private, evidence = map(Path, sys.argv[1:])
sys.path.insert(0, str(private))
import qualification_report as report


def remaining() -> float:
    value = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
    if value <= 0:
        raise TimeoutError("Retained audit recovery deadline exhausted")
    return value


def fetch(expected: dict, destination: Path) -> None:
    """Use the existing bounded CAS retrieval contract on the remote worker."""
    request = urllib.request.Request(
        "https://app.buildbuddy.io/api/v1/GetFile",
        data=json.dumps({"uri": expected["uri"]}).encode(),
        headers={"Content-Type": "application/json",
                 "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
    size = 0
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=min(60, remaining())) as response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            remaining()
            size += len(chunk)
            if size > expected["bytes"]:
                raise ValueError("Retained audit exceeded its declared size")
            digest.update(chunk)
            output.write(chunk)
    if (size, digest.hexdigest()) != (expected["bytes"], expected["sha256"]):
        raise ValueError("Retained CAS object identity differs")


def matches_output(action: dict, output: dict) -> bool:
    return any(item["path"] == output["path"]
               and item.get("digest", {}).get("hash") == output["sha256"]
               and int(item.get("digest", {}).get("sizeBytes", -1)) == output["bytes"]
               for item in action["actualOutputs"])


def recover() -> dict:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    supplier = config["retained_audits"]
    result = {"source": config["source"], "status": "INCOMPLETE", "supplier": supplier,
              "actions": [], "scope": "Supplemental proof from the original audits; no new build or runtime execution."}
    try:
        artifact = private / "retained-root-images.tar.gz"
        fetch(supplier["artifact"], artifact)
        shutil.copyfile(artifact, evidence / artifact.name)
        with tarfile.open(artifact) as archive:
            def member(name: str) -> bytes:
                entries = [entry for entry in archive.getmembers() if entry.name == name]
                match entries:
                    case [entry] if entry.isfile():
                        return archive.extractfile(entry).read()
                    case _:
                        raise ValueError("Expected one regular retained member: " + name)

            def native(phase: str) -> dict:
                row = json.loads(member("invocation-" + phase + "/verdict.json"))
                if row["source"] != supplier["source"] or row["status"] != "PASS" or row["native_exit"] != 0:
                    raise ValueError("Original native phase did not pass: " + phase)
                return row

            original = json.loads(member("verdict.json"))
            if original["status"] != "PASS" or original["sources"] != [supplier["source"]]:
                raise ValueError("Original native bundle source or result differs")
            native("root-image-closure")
            inspection = json.loads(member("root-image-contents.json"))
            if inspection["status"] != "PASS" or inspection["source"] != supplier["source"]:
                raise ValueError("Original image inspection did not pass")
            images = inspection["images"]["fsstress"]
            if images["source"] != supplier["source"]:
                raise ValueError("Original fsstress image source differs")
            manifest = json.loads(member("remote-audits.json"))
            if {row["name"] for row in supplier["audits"]} != set(manifest):
                raise ValueError("Supplied audit set differs from the original manifest")
            for architecture, cpu in (("amd64", "x86_64"), ("arm64", "aarch64")):
                native("busybox-" + cpu)
                proof = json.loads(member(cpu + "-busybox-image-execution.json"))
                if proof["status"] != "PASS" or proof["architecture"] != cpu or proof["phase"] != "busybox":
                    raise ValueError("Original audit proof differs: " + cpu)
                descriptor = proof["execution_log"]
                match [row for row in supplier["audits"] if row["name"] == descriptor["name"]]:
                    case [audit] if all(row == audit[key] for key, row in descriptor.items()):
                        pass
                    case _:
                        raise ValueError("Original audit descriptor differs: " + cpu)
                if manifest[audit["name"]] != {key: audit[key] for key in ("bytes", "sha256")}:
                    raise ValueError("Original audit manifest differs: " + cpu)
                match [row for row in images["architectures"] if row["architecture"] == architecture]:
                    case [image] if image["status"] == "PASS":
                        pass
                    case _:
                        raise ValueError("Expected one passing fsstress image: " + architecture)
                match [program for program in image["static_programs"]
                       if program["installed_path"] == "usr/bin/fsstress"]:
                    case [program] if program["owner"] == "@ltp//:fsstress":
                        output = dict(program["output"])
                    case _:
                        raise ValueError("Expected one configured fsstress program")
                match output["path"].split("/bazel-out/", 1):
                    case [_, relative]:
                        output["path"] = "bazel-out/" + relative
                    case _:
                        raise ValueError("Expected the configured fsstress output path")
                match [row for row in inspection["ltp_inputs"][architecture] if row[1] == [output["path"]]]:
                    case [[owner, _]] if owner == "@@+_repo_rules+ltp//:fsstress":
                        pass
                    case _:
                        raise ValueError("Exact LTP source-owner query differs")
                if any(matches_output(action, output) for action in proof["image_actions"]):
                    raise ValueError("Requested producer is already in the original compact proof")
                compressed = private / audit["name"]
                try:
                    fetch(audit, compressed)
                    actions = []
                    size = 0
                    digest = hashlib.sha256()
                    with gzip.open(compressed, "rt", encoding="utf-8", newline="") as stream:
                        for text, action in execution_records(stream):
                            remaining()
                            raw = text.encode()
                            size += len(raw)
                            if size > proof["uncompressed"]["bytes"]:
                                raise ValueError("Audit text exceeded its original size")
                            digest.update(raw)
                            if action is not None and action["targetLabel"] == owner:
                                summary = report.execution_summary(action)
                                if matches_output(summary, output):
                                    actions.append(summary)
                    if {"bytes": size, "sha256": digest.hexdigest()} != proof["uncompressed"]:
                        raise ValueError("Complete uncompressed audit identity differs")
                    match actions:
                        case [action] if action["exitCode"] == 0 and not action["status"] and action["digest"]["hash"]:
                            pass
                        case _:
                            raise ValueError("Expected one successful exact fsstress producer")
                    if action["runner"] == "remote" and not action["cacheHit"]:
                        properties = {item["name"]: item["value"] for item in action["platform"]["properties"]}
                        if not action["remotable"] or not proof["expected_properties"].items() <= properties.items():
                            raise ValueError("Original uncached worker properties differ")
                    result["actions"].append({
                        "architecture": architecture, "source": supplier["source"],
                        "invocation": supplier["invocation"], "audit": audit,
                        "uncompressed": proof["uncompressed"], "output": output,
                        "action": action, "cache_only_is_execution_proof": False})
                finally:
                    compressed.unlink(missing_ok=True)
            if len(result["actions"]) != 2:
                raise ValueError("Expected both architecture producer proofs")
            result["status"] = "PASS"
            return result
    finally:
        report.write_json(evidence / "retained-ltp-actions.json", result)


if __name__ == "__main__":
    result = recover()
    print(json.dumps({"status": result["status"], "recovered_actions": len(result["actions"])}))
