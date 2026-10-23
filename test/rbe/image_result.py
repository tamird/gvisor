"""Shared receipt and image-output joins for retained qualification results."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import tarfile

import qualification_report as report


@dataclass(frozen=True)
class ImageBundle:
    source: str
    members: dict[str, bytes]
    index: dict[str, dict]
    phases: dict[str, dict]


def load_retained_bundle(receipt: dict, source: str, artifact: Path) -> ImageBundle:
    """Authenticate the ordinary bundle; workload success remains caller-owned."""
    assert report.file_record(artifact) == {
        key: receipt["artifact"][key] for key in ("sha256", "bytes")}
    members = {}
    with tarfile.open(artifact) as archive:
        for member in archive:
            if member.isfile():
                assert member.name not in members, member.name
                with archive.extractfile(member) as stream:
                    members[member.name] = stream.read()
    index = {name: {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
             for name, body in sorted(members.items())}
    phases = {name: json.loads(body) for name, body in members.items()
              if name.startswith("invocation-") and name.endswith("/verdict.json")}
    assert receipt["collection_status"] == "COMPLETE" and receipt["sources"] == [source]
    assert phases and all(row["source"] == source for row in phases.values())
    return ImageBundle(source, members, index, phases)


def bind_image_outputs(
    bundle: ImageBundle,
    receipt: dict,
    config: dict,
    images: dict[str, list[dict]],
    program_counts: dict[str, int],
    event_prefix: Path,
    supplemental_actions: list[dict] | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Join configured archives/programs to BEP, native actions and remote audits.

    Callers enforce the image/architecture set, total counts and their runtime
    contracts. Each image row uses the existing remote inspector schema.
    Cache hits establish output identity, not fresh execution. The caller must
    authenticate any supplemental recovery bundle and its native verdict first.
    Supplemental rows remain separate from the original compact proof.
    """
    members, phases = bundle.members, bundle.phases
    outputs, native_outputs, audits = [], [], []
    supplements = supplemental_actions or []
    used_supplements = set()
    manifest = json.loads(members["remote-audits.json"])
    assert set(images) == set(program_counts), (set(images), set(program_counts))
    for architecture, cpu in (("amd64", "x86_64"), ("arm64", "aarch64")):
        phase = "invocation-busybox-" + cpu
        verdict = phases[phase + "/verdict.json"]
        assert {row["label"] for row in verdict["expected"]} == set(config["image_targets"][cpu])
        events = event_prefix.with_name(event_prefix.name + "-" + cpu + "-events.jsonl")
        events.write_bytes(members[phase + "/events.jsonl"])
        groups = {(report.Owner(row["label"], row["configuration"]), "default")
                  for row in verdict["expected"]}
        found = report.advertised_build_outputs(events, groups)
        assert len(found) == len(config["image_targets"][cpu])
        proof = json.loads(members[cpu + "-busybox-image-execution.json"])
        assert proof["status"] == "PASS" and proof["cache_only_is_execution_proof"] is False
        audit = proof["execution_log"]
        assert manifest[audit["name"]] == {key: audit[key] for key in ("sha256", "bytes")}
        retained = [row for row in receipt["audit_artifacts"] if row["name"] == audit["name"]]
        assert len(retained) == 1 and all(retained[0][key] == audit[key] for key in ("sha256", "bytes"))
        assert audit["name"] not in members
        audits.append({key: proof[key] for key in (
            "phase", "runner_counts", "uncached_remote_image_actions", "execution_log")})
        selected = [(name, row) for name, rows in images.items() for row in rows
                    if row["architecture"] == architecture]
        for image_name, row in selected:
            assert row["status"] == row["runtime_inputs"]["status"] == "PASS"
            assert row["runtime_inputs"]["source"] == bundle.source
            for key in ("archive", "context"):
                item = row[key]
                matches = [output for output in found if (
                    output["sha256"], output["bytes"], output["uri"]) == (
                        item["sha256"], item["bytes"], "file://" + item["path"])]
                assert len(matches) == 1, (image_name, architecture, key)
            programs = row["static_programs"] + row.get("static_utilities", [])
            assert len(programs) == program_counts[image_name], image_name
            for program in programs:
                item = program["output"]
                match item["path"].split("/bazel-out/", 1):
                    case [_, relative]:
                        output_path = "bazel-out/" + relative
                    case _:
                        raise ValueError("Expected configured program path: " + item["path"])
                def produces(action: dict) -> bool:
                    return any(
                        output["path"] == output_path
                        and output.get("digest", {}).get("hash") == item["sha256"]
                        and int(output.get("digest", {}).get("sizeBytes", -1)) == item["bytes"]
                        for output in action["actualOutputs"])

                producers = [(action, None) for action in proof["image_actions"] if produces(action)]
                for number, supplemental in enumerate(supplements):
                    if (supplemental["architecture"] == architecture and
                            supplemental["output"] == {"path": output_path, "sha256": item["sha256"], "bytes": item["bytes"]}):
                        assert supplemental["source"] == bundle.source
                        assert supplemental["invocation"] == receipt["native_invocation"]["invocationId"]
                        assert supplemental["audit"] == retained[0]
                        assert supplemental["uncompressed"] == proof["uncompressed"]
                        assert supplemental["cache_only_is_execution_proof"] is False
                        assert produces(supplemental["action"])
                        producers.append((supplemental["action"], number))
                match producers:
                    case [(action, supplemental_number)]:
                        pass
                    case _:
                        raise ValueError("Expected one producing action: " + image_name + " " + output_path)
                assert action["exitCode"] == 0 and not action["status"] and action["digest"]["hash"]
                assert len(action["full_record_sha256"]) == 64
                joined = {
                    "image": image_name, "architecture": architecture, "output": item,
                    "action": {key: action[key] for key in (
                        "digest", "targetLabel", "runner", "cacheHit", "full_record_sha256")}}
                if supplemental_number is not None:
                    used_supplements.add(supplemental_number)
                    joined["supplemental_recovery"] = supplements[supplemental_number]["recovery"]
                native_outputs.append(joined)
        outputs.extend(found)
    assert receipt["audit_upload_status"] == "PASS" and not receipt["audit_upload_errors"]
    assert used_supplements == set(range(len(supplements))), "Unmatched supplemental producer proof"
    return outputs, native_outputs, audits


def write_outcome(
    path: Path, outcome: dict[str, object], native_status: str, projection_status: str,
) -> int:
    """Write one final verdict and return its process status.

    A successful projection of failed native evidence is still a failed
    qualification. Callers must exit with this return value.
    """
    allowed = {"PASS", "FAIL", "INCOMPLETE"}
    if native_status not in allowed or projection_status not in allowed:
        raise ValueError(f"Unknown qualification verdicts: {native_status!r}, {projection_status!r}")
    status = native_status if native_status != "PASS" else projection_status
    result = {**outcome, "status": status, "native_status": native_status,
              "projection_status": projection_status}
    report.write_json(path, result)
    print(json.dumps({"path": str(path), **report.file_record(path),
                      "status": status, "native_status": native_status,
                      "projection_status": projection_status}), flush=True)
    return int(status != "PASS")
