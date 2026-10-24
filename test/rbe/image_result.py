"""Shared receipt and image-output joins for retained qualification results."""
from collections.abc import Callable
from dataclasses import dataclass
import datetime
import hashlib
import json
import re
from pathlib import Path
import tarfile
import xml.etree.ElementTree as ET

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


def bind_mixed_image_outputs(
    bundle: ImageBundle, receipt: dict, targets: list[str], graph: dict,
    aggregate_outputs: list[dict], events_path: Path,
) -> tuple[list[dict], dict]:
    """Join a mixed image build's copied outputs to their configured producers.

    The inspector supplies each aggregate's complete cquery row and the
    original output identity. The caller owns the exact image/platform set.
    """
    assert graph["source"] == bundle.source
    proof = json.loads(bundle.members["mixed-images-image-execution.json"])
    assert proof["status"] == "PASS" and proof["cache_only_is_execution_proof"] is False
    audit = proof["execution_log"]
    manifest = json.loads(bundle.members["remote-audits.json"])
    assert manifest[audit["name"]] == {key: audit[key] for key in ("sha256", "bytes")}
    retained = [row for row in receipt["audit_artifacts"] if row["name"] == audit["name"]]
    assert len(retained) == 1 and all(retained[0][key] == audit[key] for key in ("sha256", "bytes"))
    assert receipt["audit_upload_status"] == "PASS" and not receipt["audit_upload_errors"]
    verdict = bundle.phases["invocation-mixed-images/verdict.json"]
    assert verdict["status"] == "PASS" and verdict["native_exit"] == 0
    assert {row["label"] for row in verdict["expected"]} == set(targets)
    events_path.write_bytes(bundle.members["invocation-mixed-images/events.jsonl"])
    found = report.advertised_build_outputs(events_path, {
        (report.Owner(row["label"], row["configuration"]), "default") for row in verdict["expected"]})
    assert len(found) == len(aggregate_outputs)

    def native_output(item: dict) -> dict:
        prefix = graph["execution_root"] + "/"
        assert item["path"].startswith(prefix)
        relative = item["path"].removeprefix(prefix)
        assert relative.startswith("bazel-out/") and ".." not in Path(relative).parts
        actions = [action for action in proof["image_actions"] if any(
            output["path"] == relative and output.get("digest", {}).get("hash") == item["sha256"]
            and int(output.get("digest", {}).get("sizeBytes", -1)) == item["bytes"]
            for output in action["actualOutputs"])]
        match actions:
            case [action] if action["exitCode"] == 0 and not action["status"] and action["digest"]["hash"]:
                assert len(action["full_record_sha256"]) == 64
                return {key: action[key] for key in (
                    "digest", "targetLabel", "runner", "cacheHit", "full_record_sha256")}
            case _:
                raise ValueError("Missing or ambiguous native image output: " + relative)

    outputs, used = [], set()
    for row in aggregate_outputs:
        copied, original = row["copy"]["output"], row["original"]["output"]
        assert (copied["sha256"], copied["bytes"]) == (original["sha256"], original["bytes"])
        relative = copied["path"].removeprefix(graph["execution_root"] + "/")
        blob_suffix = "/blobs/" + copied["sha256"] + "/" + str(copied["bytes"])
        matches = [(number, item) for number, item in enumerate(found)
                   if (item["name"], item["sha256"], item["bytes"]) ==
                   (relative, copied["sha256"], copied["bytes"])
                   and (item["uri"] == "file://" + copied["path"] or
                        item["uri"].startswith("bytestream://") and item["uri"].endswith(blob_suffix))]
        assert len(matches) == 1
        number, item = matches[0]
        assert number not in used
        used.add(number)
        owner = row["aggregate_owner"]
        assert owner in graph["rows"] and item["identity"]["label"] == owner["label"].removeprefix("@@")
        assert copied["path"].removeprefix(graph["execution_root"] + "/") in owner["files"]
        outputs.append({**row, "copy_action": native_output(copied), "original_action": native_output(original)})
    assert used == set(range(len(found)))
    return outputs, audit


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


def verify_mixed_graph(bundle: ImageBundle, package: str, name: str) -> dict:
    """Require one archive and context owner for each declared image platform."""
    graph = json.loads(bundle.members["image-configured-inputs.json"])
    assert graph["source"] == bundle.source
    for cpu in ("k8", "aarch64"):
        for kind in ("archive", "context"):
            [owner] = [row for row in graph["rows"]
                       if row["label"].removeprefix("@@") == package + name + "_" + kind and row["cpu"] == cpu]
            assert len(owner["files"]) == 1
    return graph


def verify_mixed_oci_images(
    bundle: ImageBundle, receipt: dict, config: dict, image: dict,
    package: str, name: str, metadata_directory: Path, appended_layers: int,
    events_path: Path,
) -> tuple[list[dict], dict]:
    """Authenticate mixed outputs and unchanged upstream OCI layers/config.

    The caller checks the specific appended filesystem and runtime consumers.
    Metadata filenames are index.json and <architecture>-{manifest,config}.json.
    """
    assert image["source"] == bundle.source and image["status"] == "PASS"
    graph = verify_mixed_graph(bundle, package, name)
    outputs, audit = bind_mixed_image_outputs(
        bundle, receipt, config["targets"], graph, image["aggregate_outputs"], events_path)
    assert len(outputs) == 4 and len(receipt["audit_artifacts"]) == 1
    index_path = metadata_directory / "index.json"
    assert "sha256:" + report.file_record(index_path)["sha256"] == config["upstream_index_digest"]
    index = json.loads(index_path.read_text())
    assert [row["architecture"] for row in image["architectures"]] == ["amd64", "arm64"]
    for row in image["architectures"]:
        arch = row["architecture"]
        cpu = "k8" if arch == "amd64" else "aarch64"
        assert row["status"] == row["runtime_inputs"]["status"] == "PASS"
        assert row["runtime_inputs"]["source"] == bundle.source
        for kind in ("archive", "context"):
            [joined] = [item for item in outputs if item["architecture"] == arch and item["kind"] == kind]
            assert joined["original"]["output"] == row[kind]
            assert joined["aggregate_owner"]["cpu"] == "k8"
            [owner] = [item for item in graph["rows"] if item["label"].removeprefix("@@") == package + name + "_" + kind and item["cpu"] == cpu]
            assert owner["files"] == [row[kind]["path"].removeprefix(graph["execution_root"] + "/")]
        [selected] = [item for item in index["manifests"] if item["platform"]["os"] == "linux" and item["platform"]["architecture"] == arch]
        manifest_path = metadata_directory / (arch + "-manifest.json")
        manifest_id = report.file_record(manifest_path)
        assert ("sha256:" + manifest_id["sha256"], manifest_id["bytes"]) == (selected["digest"], selected["size"])
        manifest = json.loads(manifest_path.read_text())
        config_path = metadata_directory / (arch + "-config.json")
        config_id = report.file_record(config_path)
        assert ("sha256:" + config_id["sha256"], config_id["bytes"]) == (manifest["config"]["digest"], manifest["config"]["size"])
        upstream = json.loads(config_path.read_text())
        for key, expected in (("index", report.file_record(index_path)), ("manifest", manifest_id), ("config", config_id)):
            assert {field: row["upstream"][key][field] for field in ("sha256", "bytes")} == expected
        assert row["upstream"]["config_values"] == upstream["config"]
        for key in ("User", "ExposedPorts", "Env", "Entrypoint", "Cmd", "Volumes", "WorkingDir", "Labels", "StopSignal"):
            assert (row["config"]["config"].get(key) or None) == (upstream["config"].get(key) or None)
        inherited = list(zip(manifest["layers"], upstream["rootfs"]["diff_ids"], strict=True))
        assert row["inherited_layer_count"] == len(inherited) and len(row["layers"]) == len(inherited) + appended_layers
        assert row["files_scope"] == "appended layers" and (row["config"]["os"], row["config"]["architecture"]) == ("linux", arch)
        for actual, (layer, diff_id) in zip(row["layers"][:len(inherited)], inherited, strict=True):
            assert actual["inherited"] is True and "uncompressed_sha256" not in actual
            assert ("sha256:" + actual["sha256"], actual["bytes"], actual["expected_diff_id"]) == (layer["digest"], layer["size"], diff_id)
        for layer in row["layers"][len(inherited):]:
            assert layer["expected_diff_id"] == "sha256:" + layer["uncompressed_sha256"]
    return outputs, audit


def verify_retained_runtime(
    bundle: ImageBundle, config: dict, runtime: dict, prepared: list[dict],
    image: dict, prefix: str, events_path: Path,
) -> dict:
    """Bind every installed runtime to the same retained seven-file release."""
    assert runtime["status"] == runtime["cleanup"] == "PASS" and runtime["source"] == bundle.source
    assert runtime["container_inventory"]["before"] == runtime["container_inventory"]["after"]
    supplier = config["runtime_supplier"]
    retained = json.loads(bundle.members[prefix + "-runtime-supplier.json"])
    assert retained["status"] == "PASS" and retained["supplier"] == supplier
    assert runtime["runtime_source"] == supplier["source"]
    verdict = json.loads(bundle.members[prefix + "-runtime-supplier-verdict.json"])
    assert verdict["status"] == "PASS" and verdict["native_exit"] == 0 and verdict["source"] == supplier["source"]
    assert [row for row in verdict["expected"] if row["label"] == "//:release"] == [supplier["owner"]]
    events_path.write_bytes(bundle.members[prefix + "-runtime-supplier-events.jsonl"])
    owner = report.Owner(supplier["owner"]["label"], supplier["owner"]["configuration"])
    release = report.advertised_build_outputs(events_path, {(owner, "default")})
    assert release == retained["release_outputs"] and len(release) == 8
    by_name = {row["name"].split("/release/", 1)[1]: row for row in release}
    expected = {"runsc", "gvisor-bin/checkpointgofer", "gvisor-bin/runsc-metric-server",
                "gvisor-bin/runsc-fd-parking", "gvisor-bin/gvisor-sentry-prewarmer",
                "gvisor-bin/gvisor-proc-umounter", "gvisor-bin/gvisor_sentry"}
    assert set(by_name) == expected | {"containerd-shim-runsc-v1"}
    assert set(supplier["files"]) == set(retained["files"]) == expected
    assert prepared
    for record in prepared:
        assert record["status"] == record["cleanup"] == "PASS" and record["release_supplier"] == supplier
        assert record["runtime_inputs"] == image["runtime_inputs"]
        assert record["image"]["id"] == "sha256:" + image["config_sha256"]
        assert record["image"]["rebuild"] == record["image"]["cached_restore"] == "PASS"
        installed = {row["path"].split("/" + prefix + "-runtime/", 1)[1]: row for row in record["runtime_files"]}
        assert set(installed) == expected
        for name, item in supplier["files"].items():
            assert all(row[key] == item[key] for row in (installed[name], by_name[name], retained["files"][name]) for key in ("bytes", "sha256"))
            assert retained["files"][name]["uri"] == f"bytestream://remote.buildbuddy.io/blobs/{item['sha256']}/{item['bytes']}"
    return retained


def verify_uncached_attempt(bundle: ImageBundle, phase: str, owner: str) -> dict:
    """Require one actual local attempt of the specified configured test owner."""
    [attempt] = bundle.phases["invocation-" + phase + "/verdict.json"]["executed"]
    assert attempt["owner"]["label"] == owner
    assert not attempt["cached_locally"] and not attempt["execution_info"].get("cachedRemotely", False)
    assert attempt["execution_info"]["strategy"] == "local"
    return attempt


def retained_test_report(bundle: ImageBundle, phase: str, kind: str, record: dict) -> bytes:
    """Authenticate the exact advertised test report in the original bundle."""
    assert kind in ("xml", "log")
    [body] = [body for name, body in bundle.members.items() if name.startswith("invocation-" + phase + "/") and name.endswith("/test." + kind)
              and (len(body), hashlib.sha256(body).hexdigest()) == (record["bytes"], record["sha256"])]
    return body


def retained_test_xml(bundle: ImageBundle, phase: str, record: dict) -> ET.Element:
    """Authenticate one advertised XML record without weakening its failures."""
    body = retained_test_report(bundle, phase, "xml", record)
    xml = ET.fromstring(body)
    assert all(node.tag not in ("failure", "error", "skipped") for node in xml.iter())
    return xml


def project_retained_image(prefix: Path, manual: int, verify: Callable[[Path, ImageBundle, dict, dict, dict], dict], references: list[Path], scope: str) -> int:
    """Seal a retained image result, preserving native failures and missing data."""
    receipt, plan, config = {}, {}, {}
    bundle = None
    details, errors = {}, []
    projection = "INCOMPLETE"

    def read(suffix: str) -> dict:
        value = json.loads(Path(str(prefix) + suffix).read_text())
        if not isinstance(value, dict):
            raise ValueError("Expected an object in " + suffix)
        return value

    def descriptor(path: Path) -> dict:
        return {"path": str(path), **report.file_record(path)}

    try:
        assert manual >= 0
        receipt, plan, config = read("-receipt.json"), read("-plan.json"), read("-config.json")
        assert re.fullmatch(r"[0-9a-f]{40}", plan["source"]) and plan["source"] != "0" * 40
        assert config["source"] == plan["source"] and config["product"] == plan["product"]
        config_path = Path(str(prefix) + "-config.json")
        [config_ref] = [row for row in plan["references"] if row["path"] == str(config_path)]
        assert descriptor(config_path) == config_ref
        bundle = load_retained_bundle(receipt, plan["source"], Path(str(prefix) + "-qualification.tar.gz"))
        report.write_json(Path(str(prefix) + "-members.json"), bundle.index)
        if receipt["status"] == "PASS":
            expected = plan["graph_phases"] + ([] if config.get("graph_only", False) else plan["full_additional_phases"])
            assert len(expected) == len(set(expected)) == plan["expected_native_phases"]
            assert set(bundle.phases) == {"invocation-" + name + "/verdict.json" for name in expected}
            assert receipt["producer_status"] == "PASS"
            assert all(row["status"] == "PASS" and (row["native_exit"] == 0 or
                       row["command_kind"] == "analysis" and row["accepted_analysis_exit"] and row["native_exit"] == 1)
                       for row in bundle.phases.values())
            details = verify(prefix, bundle, receipt, plan, config)
        else:
            assert receipt["status"] in ("FAIL", "INCOMPLETE")
        projection = "INCOMPLETE" if receipt["status"] == "INCOMPLETE" else "PASS"
    except (OSError, AssertionError, KeyError, ValueError, TypeError, ET.ParseError) as error:
        errors.append(type(error).__name__ + ": " + str(error))
        projection = "INCOMPLETE" if receipt.get("status") == "INCOMPLETE" or isinstance(error, (OSError, KeyError)) else "FAIL"

    def timestamp(value):
        if not value:
            return None
        try:
            parsed = datetime.datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                raise ValueError("Naive timestamp")
            return parsed
        except (TypeError, ValueError) as error:
            errors.append("Invalid retained timestamp: " + str(error))
            return None

    phases = bundle.phases if bundle is not None else {}
    finished_values = [timestamp(row.get("execution_finished_at")) for row in phases.values() if isinstance(row, dict)]
    finished = max((value for value in finished_values if value is not None), default=None)
    verified = timestamp(receipt.get("consumer_verified_at"))
    sealed = datetime.datetime.now(datetime.timezone.utc)
    refs = []
    for path in [*[Path(str(prefix) + suffix) for suffix in ("-receipt.json", "-qualification.tar.gz", "-plan.json", "-config.json", "-submission.json")],
                 Path(__file__), *references]:
        if path.is_file():
            try:
                refs.append(descriptor(path))
            except OSError as error:
                errors.append("Cannot retain reference: " + str(error))
    if errors and projection == "PASS":
        projection = "FAIL"
    native = receipt.get("status", "INCOMPLETE")
    if native not in ("PASS", "FAIL", "INCOMPLETE"):
        errors.append("Unknown native status: " + str(native))
        native, projection = "INCOMPLETE", "FAIL"
    invocation = receipt.get("native_invocation")
    outcome = {"collection_status": receipt.get("collection_status", "INCOMPLETE"),
               "source": plan.get("source"), "product": plan.get("product"),
               "invocation": invocation.get("invocationId") if isinstance(invocation, dict) else None,
               "errors": errors, "graph_only": config.get("graph_only"),
               "native_phases": {name: {key: row.get(key) for key in ("status", "native_exit", "execution_finished_at")}
                                 for name, row in phases.items() if isinstance(row, dict)},
               "timing": {"execution_finished_at": None, "last_inner_phase_finished_at": finished.isoformat() if finished else None,
                          "consumer_verified_at": receipt.get("consumer_verified_at"), "owner_sealed_at": sealed.isoformat(),
                          "last_inner_phase_to_verified_seconds": (verified - finished).total_seconds() if verified and finished else None,
                          "verified_to_owner_seconds": (sealed - verified).total_seconds() if verified else None},
               "manual_collection_actions": manual, "references": refs, "scope": scope, **details}
    return write_outcome(Path(str(prefix) + "-outcome.json"), outcome, native, projection)
