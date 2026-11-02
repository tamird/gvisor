"""Reconcile original native unit reports without rebuilding or running tests."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import zipfile

sys.path.insert(0, str(Path("test/rbe").resolve()))
import qualification_report as report
import unit_matrix


SOURCE = "96fadde722e14d482a62276b80237398f8b62fbf"
FILTERS = ("-nogo", "-requires-kvm")
TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "unit-reconciled"


def original(arch):
    archive = TMP / (arch + ".zip")
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == os.environ[arch.upper() + "_SHA256"]
    metadata = report.read_json(TMP / (arch + "-artifact.json"))
    assert metadata["id"] == int(os.environ[arch.upper() + "_ARTIFACT"])
    assert metadata["size_in_bytes"] == archive.stat().st_size
    assert metadata["digest"] == "sha256:" + os.environ[arch.upper() + "_SHA256"]
    assert metadata["workflow_run"]["id"] == int(os.environ[arch.upper() + "_RUN"])
    assert metadata["workflow_run"]["head_sha"] == os.environ[arch.upper() + "_CARRIER"]
    destination = TMP / (arch + "-original")
    destination.mkdir()
    with zipfile.ZipFile(archive) as bundle:
        assert len(bundle.namelist()) == len(set(bundle.namelist()))
        assert sum(item.file_size for item in bundle.infolist()) < 2 * 1024**3
        for item in bundle.infolist():
            path = destination / item.filename
            assert path.resolve().is_relative_to(destination.resolve())
            assert not stat.S_ISLNK(item.external_attr >> 16)
            assert item.file_size < 512 * 1024**2
        bundle.extractall(destination)
    return destination


def source(phase):
    body = (phase / "commit_file").read_bytes()
    assert hashlib.sha1(b"commit " + str(len(body)).encode() + b"\0" + body).hexdigest() == SOURCE
    assert (phase / "final_head").read_text().strip() == SOURCE
    assert not (phase / "source_before").read_bytes() and not (phase / "source_after").read_bytes()
    manifest = report.read_json(phase / "events.jsonl.manifest.json")
    assert manifest["events"] == report.file_record(phase / "events.jsonl")
    args = (phase / "executed_arguments").read_text().splitlines()
    assert args[0] == "test"
    explicit = [arg for arg in args if arg.startswith("--test_tag_filters")]
    assert explicit == ["--test_tag_filters=" + ",".join(FILTERS)] or (
        not explicit and ("--config=unit" in args or "--config=buildkite-unit" in args))
    assert not any(arg.startswith(("--test_filter", "--test_size_filters", "--test_timeout_filters", "--test_lang_filters")) for arg in args)
    return manifest


def configured(phase):
    rows = []
    for line in (phase / "events.jsonl").read_text().splitlines():
        event = json.loads(line)
        if "configured" not in event:
            continue
        identity, detail = event["id"]["targetConfigured"], event["configured"]
        children = [child["targetCompleted"] for child in event["children"] if "targetCompleted" in child]
        assert len(children) == 1
        child, = children
        assert child["label"] == identity["label"] and child.get("aspect", "") == identity.get("aspect", "")
        rows.append((identity["label"], identity.get("aspect", ""), detail.get("targetKind", ""),
                     tuple(sorted(detail.get("tag", []))), child["configuration"]["id"]))
    assert len(rows) == len(set(rows)) == 3216
    return sorted(rows)


def selection(phase):
    manifest = source(phase)
    assert not manifest["errors"]
    analyzed = unit_matrix.configured_targets(str(phase / "events.jsonl"), tests=True, allow_test_no_build=True,
                                              expected_exit=int((phase / "native_exit").read_text()))
    selected = {label: target for label, target in analyzed.items() if unit_matrix.test_tags_match(target.tags, FILTERS)}
    excluded = {tag: sorted(label for label, target in analyzed.items() if tag in target.tags)
                for tag in ("nogo", "requires-kvm")}
    assert len(analyzed) == 1069 and len(selected) == 355
    assert len(excluded["nogo"]) == 711 and len(excluded["requires-kvm"]) == 3
    assert not set(excluded["nogo"]) & set(excluded["requires-kvm"])
    assert set(analyzed) - selected.keys() == set(excluded["nogo"] + excluded["requires-kvm"])
    rows = configured(phase)
    value = dict(analyzed_test_rules=sorted(analyzed), runnable_tests=sorted(selected), excluded=excluded,
                 configured=rows, test_tag_filters=FILTERS, native_analysis_exit=int((phase / "native_exit").read_text()))
    return value, selected


def execution(phase, labels, name, selected=None):
    source(phase)
    expected = OUT / (name + "-targets.txt")
    expected.write_text("\n".join(sorted(labels)) + "\n")
    context = report.read_json(phase / "context.json")
    assert context["expected_source"] == SOURCE
    verdict = report.verdict(phase / "events.jsonl", phase / "events.jsonl.manifest.json", phase / "context.json",
                             expected, test_tag_filters=FILTERS)
    report.write_json(OUT / (name + ".json"), verdict)
    actual = {(row["owner"]["label"], row["owner"]["configuration"]) for row in verdict["executed"]}
    if selected is not None:
        assert actual <= {(label, target.configuration) for label, target in selected.items()}
    invalid = [row for row in verdict["executed"] if row["cached_locally"] or row["attempt"] != 1 or row["run"] != 1 or
               row["execution_info"].get("cachedRemotely") or
               row["execution_info"].get("strategy") not in ("local", "linux-sandbox", "processwrapper-sandbox")]
    assert not invalid, invalid
    return verdict


def configuration_groups(reference, observed):
    """Match configured owner partitions without equating configuration hashes."""
    groups = []
    for rows in (reference, observed):
        by_config = {}
        for label, aspect, kind, tags, config in rows:
            by_config.setdefault(config, []).append((label, aspect, kind, tags))
        by_members = {tuple(sorted(members)): config for config, members in by_config.items()}
        assert len(by_members) == len(by_config), "Ambiguous configuration partitions"
        groups.append(by_members)
    before, after = groups
    assert before.keys() == after.keys(), "Configured owner partitions differ"
    return [dict(reference=before[members], observed=after[members], records=len(members)) for members in sorted(before)]


def rejection_controls(phase):
    context = report.read_json(phase / "context.json")
    wrong = phase / "wrong-context.json"
    report.write_json(wrong, dict(context, expected_source="0" * 40))
    value = report.verdict(phase / "events.jsonl", phase / "events.jsonl.manifest.json", wrong)
    assert value["status"] == "INCOMPLETE" and any("Commit body identifies" in error for error in value["errors"])
    report.write_json(wrong, {key: value for key, value in context.items() if key != "expected_source"})
    try:
        report.verdict(phase / "events.jsonl", phase / "events.jsonl.manifest.json", wrong)
    except ValueError:
        pass
    else:
        raise AssertionError("Missing source accepted")
    missing = phase / "missing-owner.txt"
    missing.write_text("//pkg/buffer:buffer_test\n//runsc/cgroup:cgroup_test\n//missing:owner\n")
    value = report.verdict(phase / "events.jsonl", phase / "events.jsonl.manifest.json", phase / "context.json", missing)
    assert value["status"] == "INCOMPLETE" and any(row.get("label") == "//missing:owner" for row in value["missing"])
    return ["wrong source rejected", "missing source rejected", "missing owner rejected"]


def build_coverage(phase, selection, analysis):
    expected = {(label, aspect, config) for label, aspect, _, _, config in selection}
    skipped = set()
    for line in (analysis / "events.jsonl").read_text().splitlines():
        event = json.loads(line)
        if event.get("aborted", {}).get("reason") == "SKIPPED":
            owner = event["id"]["targetCompleted"]
            skipped.add((owner["label"], owner.get("aspect", ""), owner["configuration"]["id"]))
    assert skipped <= expected
    expected -= skipped
    completed = {}
    for line in (phase / "events.jsonl").read_text().splitlines():
        event = json.loads(line)
        if "completed" in event:
            owner = event["id"]["targetCompleted"]
            key = owner["label"], owner.get("aspect", ""), owner["configuration"]["id"]
            assert key not in completed
            completed[key] = event["completed"].get("success", False)
    assert not completed.keys() - expected
    value = dict(expected=sorted(expected), analysis_skipped=sorted(skipped), completed=sorted(completed.items()), missing=sorted(expected - completed.keys()),
                 failed=sorted(key for key, success in completed.items() if not success))
    report.write_json(OUT / "arm64-build-roots.json", value)
    return value


def main():
    OUT.mkdir()
    result = dict(source=SOURCE, status="INCOMPLETE", errors=[], replay_started_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    report.write_json(OUT / "verdict.json", result)
    rc = subprocess.check_output(["git", "show", SOURCE + ":.bazelrc"], text=True)
    stanza, = [line for line in rc.splitlines() if line.startswith("test:unit ")]
    flags = [word for word in stanza.split() if word.startswith("--test_tag_filters=")]
    assert flags == ["--test_tag_filters=" + ",".join(FILTERS)]
    assert "--config=unit" in next(line for line in rc.splitlines() if line.startswith("test:buildkite-unit ")).split()
    # Nogo and syscall configs have independent filters; only check this chain.
    active = {scope + suffix for scope in ("common", "build", "test")
              for suffix in ("", ":ci", ":buildkite", ":unit", ":buildkite-unit")}
    assert sum("--test_tag_filters=" in line for line in rc.splitlines()
               if line.split() and line.split()[0] in active) == 1
    result["source_test_filter_stanza"] = stanza
    selections = {}
    for arch in ("amd64", "arm64"):
        try:
            root = original(arch)
            original_verdict = report.read_json(root / "verdict.json")
            row = result[arch] = dict(original_verdict=original_verdict,
                                     original_archive_sha256=os.environ[arch.upper() + "_SHA256"],
                                     original_run=os.environ[arch.upper() + "_RUN"],
                                     original_artifact=os.environ[arch.upper() + "_ARTIFACT"],
                                     original_carrier=os.environ[arch.upper() + "_CARRIER"])
            original_run = report.read_json(TMP / (arch + "-run.json"))
            assert original_run["id"] == int(row["original_run"]) and original_run["head_sha"] == row["original_carrier"]
            assert original_run["status"] == "completed"
            row["original_actions_conclusion"] = original_run["conclusion"]
            row["original_run_updated_at"] = original_run["updated_at"]
            phases = ("legacy", "make", "native") if arch == "amd64" else ("analysis",)
            observed = []
            for name in phases:
                inventory, selected = selection(root / name)
                report.write_json(OUT / (arch + "-" + name + "-selection.json"), inventory)
                observed.append(inventory)
            for value in observed:
                assert all(value[key] == observed[0][key] for key in ("analyzed_test_rules", "runnable_tests", "excluded", "test_tag_filters"))
            row["configuration_group_correspondence"] = {
                name: configuration_groups(observed[0]["configured"], value["configured"])
                for name, value in zip(phases, observed, strict=True)}
            selections[arch] = observed[0]["runnable_tests"]
            row.update(analyzed_test_rules=1069, runnable_tests=355, excluded_nogo=711, excluded_requires_kvm=3,
                       configured_records=3216, selection_verified=True)
            labels = selected if arch == "arm64" else ("//pkg/buffer:buffer_test", "//runsc/cgroup:cgroup_test")
            runtime = execution(root / ("full" if arch == "arm64" else "named-cases"), labels, arch + "-execution",
                                selected if arch == "arm64" else None)
            row.update(native_exit=runtime["native_exit"], test_status=runtime["status"],
                       test_attempts=len(runtime["executed"]), case_counts=runtime["case_counts"], missing=runtime["missing"],
                       failures=runtime["failures"],
                       coverage_gaps={key: runtime[key] for key in ("zero_case_owners", "all_skipped_owners")},
                       execution_finished_at=runtime["execution_finished_at"])
            if arch == "arm64":
                row["configurations"] = report.read_json(root / "analysis/configurations.json")
                assert set(row["configurations"]) == {target.configuration for target in selected.values()}
                assert all(config["cpu"] == "aarch64" for config in row["configurations"].values())
                assert configured(root / "full") == observed[0]["configured"]
                build = build_coverage(root / "full", observed[0]["configured"], root / "analysis")
                row["build_coverage"] = {key: build[key] for key in ("missing", "failed", "analysis_skipped")}
                row["build_coverage"].update(expected=len(build["expected"]), completed=len(build["completed"]))
                if runtime["native_exit"] == 0:
                    assert not build["missing"] and not build["failed"]
                row["functional_coverage"] = ("COMPLETE" if runtime["status"] == "PASS" and
                    {item["owner"]["label"] for item in runtime["executed"]} == set(selected) and
                    not any(row["coverage_gaps"].values()) and not build["missing"] and not build["failed"] else "INCOMPLETE")
                row["host"] = report.read_json(root / "host.json")
                assert row["host"]["machine"] == "aarch64" and row["host"]["uid"] != 0
            else:
                assert runtime["status"] == "PASS"
                row["rejection_controls"] = rejection_controls(root / "named-cases")
        except Exception as error:
            result["errors"].append(f"{arch}: {error!r}")
    if len(selections) == 2:
        if selections["amd64"] != selections["arm64"]:
            result["errors"].append("AMD64 and ARM64 runnable owner identities differ")
    result["status"] = "INCOMPLETE" if result["errors"] else "RECONCILED"
    result["verdict_available_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    report.write_json(OUT / "verdict.json", result)
    print(json.dumps({key: result[key] for key in ("status", "errors", "verdict_available_at")}))
    if result["status"] != "RECONCILED":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
