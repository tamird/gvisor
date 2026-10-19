# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Extract production Go packages using declared Bazel package facts."""

import argparse
from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import Literal, TypedDict, cast
import zipfile


_SOURCE_FIELDS = ("GoFiles", "CompiledGoFiles", "OtherFiles")


class Module(TypedDict):
    Path: str
    Dir: str
    GoMod: str
    Main: bool
    GoVersion: str


class Package(TypedDict, total=False):
    ID: str
    PkgPath: str
    Dir: str
    CompiledGoFiles: list[str]
    GoFiles: list[str]
    OtherFiles: list[str]
    Module: Module


class DeclaredInput(TypedDict):
    path: str
    owner: str | None
    short_path: str
    is_source: bool
    is_directory: bool


@dataclass(frozen=True)
class SourceFile:
    owner: str | None
    short_path: str
    generated: bool
    sha256: str
    size: int


@dataclass
class PackageSelection:
    records: dict[str, Package]
    variants: list[dict[str, object]]
    equivalents: dict[str, str]


class Owner(TypedDict):
    owner: str
    package_id: str
    goos: str
    goarch: str
    cgo: bool
    original_sources: list[str]


class ConfiguredOwner(TypedDict):
    owner: str
    modes: list[str]
    sources: list[str]


class DriverResponse(TypedDict):
    Roots: list[str]
    Packages: list[Package]


class Manifest(TypedDict):
    labels: list[str]
    package_json: list[str]
    stdlib_json: list[str]
    go_mod: str
    goarch: str
    goos: str
    bazel_version_file: str
    owners: list[Owner]
    declared_inputs: list[DeclaredInput]
    excluded_owners: list[ConfiguredOwner]


def json_records(path: Path) -> list[Package]:
    """Decode the upstream concatenated go-list/pkgjson record format."""
    text = path.read_text()
    decoder = json.JSONDecoder()
    records = []
    while text := text.lstrip():
        record, end = decoder.raw_decode(text)
        match record:
            case {"ID": str(), "PkgPath": str()}:
                if not isinstance(record.get("Dir", ""), str) or not isinstance(record.get("CompiledGoFiles", []), list):
                    raise ValueError(f"Invalid package paths in {path}")
                for field in _SOURCE_FIELDS:
                    files = record.get(field, [])
                    if not isinstance(files, list) or any(not isinstance(name, str) for name in files):
                        raise ValueError(f"Invalid {field} paths in {path}")
            case _:
                raise ValueError(f"Invalid package identity in {path}")
        records.append(cast(Package, record))
        text = text[end:]
    return records


def resolve_input(name: str, root: Path) -> Path:
    for placeholder in ("__BAZEL_WORKSPACE__", "__BAZEL_EXECROOT__", "__BAZEL_OUTPUT_BASE__"):
        name = name.replace(placeholder, str(root))
    return Path(name).resolve()


def source_files(inputs: list[DeclaredInput], root: Path) -> dict[str, SourceFile]:
    """Index only declared files and leaves of declared tree artifacts."""
    result: dict[str, SourceFile] = {}
    for item in inputs:
        match item:
            case {"path": str() as name, "owner": owner, "short_path": str() as short_path,
                  "is_source": bool() as original, "is_directory": bool() as directory} if owner is None or isinstance(owner, str):
                pass
            case _:
                raise ValueError(f"Invalid declared source provenance: {item}")
        path = root / name
        if not path.exists() or path.is_dir() != directory:
            raise ValueError(f"Missing or wrong-kind declared package input: {name}")
        leaves = sorted(path.rglob("*")) if directory else [path]
        for leaf in leaves:
            if not leaf.is_file():
                continue
            logical_path = str(Path(short_path) / leaf.relative_to(path)) if directory else short_path
            data = leaf.read_bytes()
            facts = SourceFile(owner, logical_path, not original, hashlib.sha256(data).hexdigest(), len(data))
            key = str(leaf.resolve())
            if (previous := result.get(key)) is not None and previous != facts:
                raise ValueError(f"Ambiguous declared source identity: {key}: {previous} != {facts}")
            result[key] = facts
    return result


def selected_packages(records: list[tuple[str, Package]], inputs: dict[str, SourceFile], root: Path,
                      *, group_by: Literal["ID", "PkgPath"]) -> PackageSelection:
    """Reconcile configured IDs or actual loaded package paths with one contract."""
    selected: dict[str, Package] = {}
    representatives: dict[str, tuple[Package, str]] = {}
    variants: list[dict[str, object]] = []
    equivalents: dict[str, str] = {}
    conflicts: list[dict[str, object]] = []
    for name, record in sorted(records, key=lambda item: item[0]):
        identity, package_path = record["ID"], record["PkgPath"]
        if package_path == "testmain":
            raise ValueError(f"Test package selected for production analysis: {identity}")
        if not record.get("Dir"):
            raise ValueError(f"Missing declared package directory: {identity}")
        for field in _SOURCE_FIELDS:
            if missing := [path for path in record.get(field, []) if str(resolve_input(path, root)) not in inputs]:
                conflicts.append({"id": identity, "metadata": name, "field": field, "undeclared_sources": missing})
        if (previous_id := selected.get(identity)) is not None and previous_id["PkgPath"] != package_path:
            conflicts.append({"id": identity, "metadata": name,
                              "package_paths": [previous_id["PkgPath"], package_path]})
        selected.setdefault(identity, record)
        if (representative := representatives.get(record[group_by])) is None:
            representatives[record[group_by]] = record, name
            continue
        previous, previous_path = representative
        differences = {}
        copies = {}
        for field in sorted(previous.keys() | record.keys()):
            # CodeQL selects one AST variant per PkgPath. Keep all IDs and
            # imports, but prove every variant has the same source semantics.
            # The effective source-loading mode does not use export data.
            if field in ("ID", "ExportFile") or (field in previous and field in record
                    and json.dumps(previous[field], sort_keys=True, allow_nan=False)
                    == json.dumps(record[field], sort_keys=True, allow_nan=False)):
                continue
            first, current = previous.get(field), record.get(field)
            if field in _SOURCE_FIELDS and isinstance(first, list) and isinstance(current, list) and len(first) == len(current):
                field_copies = {}
                unequal = []
                for old_name, new_name in zip(first, current, strict=True):
                    if old_name == new_name:
                        continue
                    old_path, new_path = str(resolve_input(old_name, root)), str(resolve_input(new_name, root))
                    old_file, new_file = inputs.get(old_path), inputs.get(new_path)
                    if old_file is None or new_file is None or not old_file.generated or not old_file.owner or old_file != new_file:
                        unequal.append({"previous": old_name, "current": new_name,
                                        "previous_facts": asdict(old_file) if old_file else None,
                                        "current_facts": asdict(new_file) if new_file else None})
                    else:
                        field_copies[new_path] = old_path
                if not unequal:
                    copies.update(field_copies)
                    continue
                differences[field] = unequal
            else:
                # Preserve absent versus explicitly null values and future fields.
                differences[field] = {
                    "previous": {field: first} if field in previous else {},
                    "current": {field: current} if field in record else {},
                }
        variants.append({"id": identity, "package_path": package_path, "group_by": group_by,
                         "selected_metadata": previous_path, "metadata": name,
                         "selected": deepcopy(previous), "record": deepcopy(record),
                         "equivalent_sources": copies})
        if differences:
            conflicts.append({"id": identity, "package_path": package_path,
                              "selected_metadata": previous_path, "metadata": name,
                              "differences": differences})
        else:
            for path, representative_path in copies.items():
                if path in equivalents and equivalents[path] != representative_path:
                    conflicts.append({"id": identity, "path": path,
                                      "representatives": [equivalents[path], representative_path]})
                equivalents[path] = representative_path
    if conflicts:
        raise ValueError("Conflicting configured package records: " + json.dumps(conflicts, sort_keys=True))
    return PackageSelection(selected, variants, equivalents)


def loaded_packages(records: list[tuple[str, Package]], package_ids: set[str],
                    inputs: dict[str, SourceFile], root: Path) -> PackageSelection:
    """Check only original records reached by the canonical driver's import walk."""
    if unknown := package_ids - {record["ID"] for _, record in records}:
        raise ValueError(f"Driver returned undeclared package identities: {sorted(unknown)}")
    return selected_packages([(name, record) for name, record in records if record["ID"] in package_ids],
                             inputs, root, group_by="PkgPath")


@dataclass
class ArchiveCoverage:
    expected: dict[str, str]
    actual: dict[str, str]
    missing: list[str]
    mismatched: list[str]
    unexpected: list[str]


def archive_coverage(expected: dict[str, str], actual: dict[str, str], equivalents: dict[str, str]) -> ArchiveCoverage:
    """Compare only source classes proved equivalent by package selection."""
    mismatched = set()
    normalized = []
    for sources in (expected, actual):
        classes: dict[str, str] = {}
        for path, digest in sorted(sources.items()):
            representative = equivalents.get(path, path)
            if representative in classes and classes[representative] != digest:
                mismatched.add(representative)
            classes.setdefault(representative, digest)
        normalized.append(classes)
    expected_classes, actual_classes = normalized
    mismatched.update(name for name in expected_classes.keys() & actual_classes.keys()
                      if expected_classes[name] != actual_classes[name])
    return ArchiveCoverage(expected_classes, actual_classes,
                           sorted(expected_classes.keys() - actual_classes.keys()),
                           sorted(mismatched), sorted(actual_classes.keys() - expected_classes.keys()))


def default_roots(owners: list[Owner], records: dict[str, Package], packages: list[Package],
                  inputs: dict[str, SourceFile], root: Path) -> tuple[list[str], list[dict[str, object]]]:
    """Retain default-filtered roots, accounting for every original root source."""
    filtered = {record["ID"]: record for record in packages}
    if len(filtered) != len(packages):
        raise ValueError("Driver duplicated package identity")
    labels = set()
    excluded = []
    for owner in owners:
        identity = owner["package_id"]
        raw, loaded = records[identity], filtered[identity]
        originals = {str((root / name).resolve()) for name in owner["original_sources"]}
        raw_files = {str(resolve_input(name, root)) for name in raw.get("GoFiles", [])}
        prefilter_files = {str(resolve_input(name, root)) for name in raw.get("CompiledGoFiles", [])}
        if missing := originals - raw_files:
            raise ValueError(f"Root metadata omitted original sources: {owner['owner']}: {sorted(missing)}")
        if not owner["cgo"] and (missing := originals - prefilter_files):
            raise ValueError(f"Root prefilter metadata omitted original sources: {owner['owner']}: {sorted(missing)}")
        files = loaded.get("CompiledGoFiles")
        if not isinstance(files, list) or any(not isinstance(name, str) for name in files):
            raise ValueError(f"Driver returned invalid root source paths: {identity}")
        if files:
            labels.add(identity)
            continue
        # An initially empty or generated-only declaration is not evidence of
        # a package excluded by the canonical architecture/build-tag filter.
        if (not originals or raw_files != originals or prefilter_files != originals
                or any(name not in inputs or inputs[name].generated for name in originals)):
            raise ValueError(f"Empty root lacks complete original source exclusion evidence: {identity}")
        excluded.append({"owner": owner, "reason": "no-default-sources", "sources": sorted(originals),
                         "raw_go_files": raw["GoFiles"], "raw_compiled_go_files": raw["CompiledGoFiles"],
                         "constraints": {name: [line for line in Path(name).read_text().splitlines()
                                                if line.startswith(("//go:build ", "// +build "))]
                                         for name in sorted(originals)}})
    if not labels:
        raise ValueError("Default source selection has no roots")
    return sorted(labels), excluded


def configured_source_parity(excluded: list[ConfiguredOwner], loaded_records: dict[str, Package],
                             eligible: set[str], inputs: dict[str, SourceFile], root: Path) -> list[dict[str, object]]:
    """Account for configured originals through actual loaded default packages."""
    membership: dict[str, list[str]] = {}
    for identity, record in loaded_records.items():
        for name in record.get("GoFiles", []):
            membership.setdefault(str(resolve_input(name, root)), []).append(identity)
    dispositions = []
    for owner in excluded:
        modes = owner["modes"]
        if not modes or len(modes) != len(set(modes)) or set(modes) - {"race", "coverage"}:
            raise ValueError(f"Invalid configured source disposition: {owner}")
        sources = owner["sources"]
        if not sources or len(sources) != len(set(sources)):
            raise ValueError(f"Configured owner has no unique original source membership: {owner}")
        evidence = []
        for name in sources:
            path = str((root / name).resolve())
            facts = inputs.get(path)
            if facts is None or facts.generated or path not in membership:
                raise ValueError(f"Configured owner loses a baseline source: {owner['owner']}: {name}")
            evidence.append({"path": path, "facts": asdict(facts), "packages": sorted(membership[path]),
                             "default_eligible": path in eligible})
        dispositions.append({**owner, "sources": evidence})
    return dispositions


def driver_response(driver: Path, labels: list[str], environment: dict[str, str], path: Path) -> DriverResponse:
    with path.open("w") as response_file:
        subprocess.run([str(driver.absolute()), *labels], input=json.dumps({"Mode": 0, "Tests": False}),
                       text=True, env=environment, stdout=response_file, check=True)
    match json.loads(path.read_text()):
        case {"Compiler": "gc", "Arch": "amd64", "Roots": list() as roots, "Packages": list() as packages} as response:
            if not all(isinstance(label, str) for label in roots):
                raise ValueError("Driver returned a non-string root identity")
        case _:
            raise ValueError("Driver did not return its declared graph and gc/amd64 type-size identity")
    if set(roots) != set(labels) or len(roots) != len(labels):
        raise ValueError("Driver roots differ from the exact production selection: " + json.dumps({
            "missing": sorted(set(labels) - set(roots)), "unexpected": sorted(set(roots) - set(labels)),
            "duplicates": len(roots) != len(set(roots)),
        }))
    return cast(DriverResponse, response)


def extract(args: argparse.Namespace) -> Path:
    root = Path.cwd()
    output = args.output.absolute()
    output.mkdir(exist_ok=True)
    # The owning rule generates this manifest in the same action graph.
    manifest = cast(Manifest, json.loads(args.manifest.read_text()))
    go_mod = Path(manifest["go_mod"]).absolute()
    module_lines = go_mod.read_text().splitlines()
    match [line.split(maxsplit=1)[1] for line in module_lines if line.startswith("module ")]:
        case [module_path]:
            pass
        case _:
            raise ValueError("Expected one declared module directive")
    match [line.split(maxsplit=1)[1] for line in module_lines if line.startswith("go ")]:
        case [go_version]:
            pass
        case _:
            raise ValueError("Expected one declared module Go version")
    module: Module = {"Path": module_path, "Dir": str(go_mod.parent), "GoMod": str(go_mod), "Main": True, "GoVersion": go_version}

    if (manifest["goos"], manifest["goarch"]) != ("linux", "amd64") or any(
            (owner.get("goos"), owner.get("goarch")) != (manifest["goos"], manifest["goarch"])
            for owner in manifest["owners"]):
        raise ValueError("Declared roots differ from the Linux/AMD64 loader and type-size contract")
    inputs = source_files(manifest["declared_inputs"], root)
    records = [(name, record) for name in sorted(manifest["package_json"]) for record in json_records(Path(name))]
    # Registry insertion is keyed by ID: reconcile configured copies first.
    selection = selected_packages(records, inputs, root, group_by="ID")
    selected = selection.records
    if missing := set(manifest["labels"]) - selected.keys():
        raise ValueError(f"Missing requested package records: {sorted(missing)}")

    raw_sources = set()
    augmented_records = []
    for record in selected.values():
        if record["ID"].startswith("@@//"):
            raw_sources.update(str(resolve_input(name, root)) for name in record["CompiledGoFiles"])
            record = {**record, "Module": module}
        augmented_records.append(record)
    packages_path = output / "packages.json"
    packages_path.write_text("\n".join(json.dumps(record) for record in augmented_records) + "\n")
    stdlib_paths = [Path(name).absolute() for name in manifest["stdlib_json"]]
    if len(stdlib_paths) != 1:
        raise ValueError("Expected one analysis-configured standard-library metadata input")
    stdlib_ids = set()
    for path in stdlib_paths:
        for record in json_records(path):
            stdlib_ids.add(record["ID"])
            if not record.get("Dir"):
                raise ValueError(f"Standard package lacks its source directory: {record['ID']}")
    driver_manifest = output / "driver.json"
    driver_configuration = {
        "JSONFiles": [str(path) for path in stdlib_paths] + [str(packages_path)],
        "Labels": manifest["labels"],
        "BazelVersion": Path(manifest["bazel_version_file"]).read_text().strip(),
        "Workspace": str(root),
        "ExecRoot": str(root),
        "OutputBase": str(root),
        "Arch": manifest["goarch"],
        "Tests": False,
    }
    driver_manifest.write_text(json.dumps(driver_configuration, indent=2) + "\n")
    environment = dict(os.environ)
    environment.update(
        GOPACKAGESDRIVER=str(args.driver.absolute()),
        GOPACKAGESDRIVER_JSON_MANIFEST=str(driver_manifest),
        PATH=str(args.go.absolute().parent) + os.pathsep + environment.get("PATH", "/usr/bin:/bin"),
        GOROOT=str(args.goroot.absolute()),
        GOOS=manifest["goos"],
        GOARCH=manifest["goarch"],
        GOPROXY="off",
        GOSUMDB="off",
        CODEQL_EXTRACTOR_GO_OPTION_EXTRACT_TESTS="false",
        # Go extraction covers declared Go inputs, not HTML in the toolchain tree.
        CODEQL_EXTRACTOR_GO_EXTRACT_HTML="no",
        GOTAGS="",
    )
    # Reuse the driver's architecture and tag filtering with public CI defaults.
    # The first response proves source filtering for every declared candidate.
    response_path = output / "driver-response.json"
    response = driver_response(args.driver, manifest["labels"], environment, output / "candidate-response.json")
    labels, empty_roots = default_roots(manifest["owners"], selected, response["Packages"], inputs, root)
    driver_configuration["Labels"] = labels
    driver_manifest.write_text(json.dumps(driver_configuration, indent=2) + "\n")
    # Reuse canonical import reachability after removing proved empty roots.
    if empty_roots:
        response = driver_response(args.driver, labels, environment, response_path)
    else:
        response_path.write_text(json.dumps(response) + "\n")
    roots, packages = response["Roots"], response["Packages"]
    eligible: dict[str, set[str]] = {}
    loaded_ids = set()
    for record in packages:
        match record:
            case {"ID": str() as identity, "Dir": str() as directory} if directory:
                files = record.get("CompiledGoFiles", [])
                if not isinstance(files, list) or any(not isinstance(name, str) for name in files):
                    raise ValueError(f"Driver returned invalid source paths: {identity}")
            case _:
                raise ValueError("Driver lost package directory or source identity")
        if identity in loaded_ids:
            raise ValueError(f"Driver duplicated package identity: {identity}")
        loaded_ids.add(identity)
        if identity.startswith("@@//"):
            observed_module = record.get("Module")
            if (not isinstance(observed_module, dict)
                    or any(observed_module.get(key) != value for key, value in module.items())
                    or observed_module.get("Version")
                    or observed_module.get("Replace") is not None
                    or observed_module.get("Error") is not None):
                raise ValueError(f"Driver lost declared module provenance: {identity}")
            eligible[identity] = {str(Path(name).resolve()) for name in files}
    # Aspect inputs include embedded fragments which are not separate imported
    # packages. The upstream driver owns reachability and build-tag filtering.
    # Compare original records for its actual IDs so unknown fields stay strict.
    loaded = loaded_packages(records, loaded_ids - stdlib_ids, inputs, root)
    excluded_records = [{"metadata": name, "record": record} for name, record in records
                        if record["ID"] not in loaded_ids]
    eligible_sources = set().union(*eligible.values())
    configured_dispositions = configured_source_parity(manifest["excluded_owners"], loaded.records,
                                                       eligible_sources, inputs, root)
    if introduced := eligible_sources - raw_sources:
        raise ValueError(f"Driver introduced undeclared source files: {sorted(introduced)}")
    expected = {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in eligible_sources}
    expected[str(go_mod.resolve())] = hashlib.sha256(go_mod.read_bytes()).hexdigest()
    (output / "expected-files.json").write_text(json.dumps(expected, indent=2) + "\n")
    declared_inputs = {name: facts.sha256 for name, facts in inputs.items()}
    equivalent_sources = {
        name: {"representative": representative, "facts": asdict(inputs[name]),
               "eligible": representative in eligible_sources}
        for name, representative in loaded.equivalents.items()
    }
    database = output / "database"
    # CodeQL removes one leading @ from each @@ command argument. Escape the
    # canonical labels once so the strict driver receives their original bytes.
    command = [str(args.extractor.absolute()), "--source-root", str(root), *("@" + label for label in labels)]
    with (output / "extraction.log").open("w") as log:
        result = subprocess.run([
            str(args.codeql.absolute()), "database", "create", str(database),
            "--language=go", "--source-root=" + str(root),
            "--command=" + shlex.join(command), "--threads=4", "--ram=12288",
            "--common-caches=" + str(output / "cache"),
        ], env=environment, stdout=log, stderr=subprocess.STDOUT)
    extraction_log = (output / "extraction.log").read_text()
    print(extraction_log, end="", flush=True)
    result.check_returncode()
    # SARIF export describes a failed analysis even when database creation
    # succeeded. Assess the raw diagnostic records and the actual process exit.
    diagnostics = output / "diagnostics.json"
    subprocess.run([
        str(args.codeql.absolute()), "database", "export-diagnostics",
        "--format=raw", "--output=" + str(diagnostics), str(database),
    ], env=environment, check=True)
    diagnostic_report = json.loads(diagnostics.read_text())
    if not isinstance(diagnostic_report, list):
        raise ValueError("CodeQL raw diagnostics must be a JSON list")
    extraction_errors = []
    for diagnostic in diagnostic_report:
        match diagnostic:
            case {"source": {"id": str() as identity, "name": str() as name}, "timestamp": str() as timestamp} if identity and name and timestamp:
                pass
            case _:
                raise ValueError("CodeQL returned an invalid raw diagnostic")
        # CLI telemetry can omit severity; the Go extractor always supplies it.
        severity = diagnostic.get("severity")
        if severity not in ("error", "warning", "note") and not ("severity" not in diagnostic and identity.startswith("cli/")):
            raise ValueError(f"CodeQL returned an invalid diagnostic severity: {identity}")
        if severity == "error":
            extraction_errors.append(diagnostic)
    # The pinned extractor also logs package-loading/type errors while continuing
    # extraction; those can be database diagnostics rather than CLI notifications.
    # Do not mistake a complete source archive for a complete typed extraction.
    package_error_lines = [line for line in extraction_log.splitlines() if "Warning: encountered errors extracting package `" in line]
    actual = {}
    with zipfile.ZipFile(database / "src.zip") as archive:
        for entry in archive.infolist():
            if entry.is_dir():
                continue
            path = "/" + entry.filename.lstrip("/")
            actual[path] = hashlib.sha256(archive.read(entry)).hexdigest()
    coverage = archive_coverage(expected, actual, loaded.equivalents)
    outcome = {
        "requested_roots": labels,
        "candidate_roots": manifest["labels"],
        "owners": manifest["owners"],
        "configured_dispositions": configured_dispositions,
        "empty_default_roots": empty_roots,
        "stdlib_json": str(stdlib_paths[0]),
        "stdlib_sha256": hashlib.sha256(stdlib_paths[0].read_bytes()).hexdigest(),
        "extract_tests": False,
        "package_records": len(selected),
        "driver_roots": roots,
        "driver_package_records": len(packages),
        "goos": manifest["goos"],
        "goarch": manifest["goarch"],
        "tags": environment["GOTAGS"],
        "raw_to_eligible_exclusions": sorted(raw_sources - eligible_sources),
        "declared_inputs": declared_inputs,
        "declared_source_provenance": {name: asdict(facts) for name, facts in inputs.items()},
        "configured_package_variants": selection.variants,
        "loaded_package_variants": loaded.variants,
        "unreachable_metadata_records": excluded_records,
        "loaded_package_ids": sorted(loaded_ids),
        "equivalent_sources": equivalent_sources,
        "module": module,
        "source_coverage_unit": "accepted package source equivalence class",
        "expected_files": len(coverage.expected),
        "actual_files": len(coverage.actual),
        "missing": coverage.missing,
        "mismatched": coverage.mismatched,
        "unexpected": coverage.unexpected,
        "expected_sha256": coverage.expected,
        "actual_sha256": coverage.actual,
        "raw_expected_files": len(expected),
        "raw_actual_files": len(actual),
        "raw_expected_sha256": expected,
        "raw_actual_sha256": actual,
        "diagnostics": diagnostic_report,
        "extraction_errors": extraction_errors,
        "package_error_lines": package_error_lines,
    }
    (output / "outcome.json").write_text(json.dumps(outcome, indent=2) + "\n")
    print(json.dumps({
        "owners": len(manifest["owners"]),
        "declared_owners": len(manifest["owners"]) + len(configured_dispositions),
        "configured_dispositions": len(configured_dispositions),
        "empty_default_roots": len(empty_roots),
        "analyzed_roots": len(labels),
        "package_records": len(selected),
        "expected_files": len(coverage.expected),
        "actual_files": len(coverage.actual),
        "declared_inputs": len(declared_inputs),
        "configured_variants": len(selection.variants),
        "loaded_variants": len(loaded.variants),
        "unreachable_metadata_records": len(excluded_records),
        "equivalent_generated_sources": len(equivalent_sources),
        "raw_to_eligible_exclusions": len(raw_sources - eligible_sources),
        "missing": len(coverage.missing),
        "mismatched": len(coverage.mismatched),
        "unexpected": len(coverage.unexpected),
        "extraction_errors": len(extraction_errors),
        "package_error_lines": len(package_error_lines),
    }), flush=True)
    if coverage.missing or coverage.mismatched or coverage.unexpected:
        raise ValueError("Extracted source coverage differs from declared package inputs")
    if extraction_errors or package_error_lines:
        raise ValueError("CodeQL reported incomplete or failed package extraction")
    return database
