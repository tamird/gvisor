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

"""Check actual CodeQL extraction against a small declared package graph."""

import argparse
from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import TypedDict, cast
import zipfile


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
    Module: Module


class Manifest(TypedDict):
    labels: list[str]
    package_json: list[str]
    stdlib_json: list[str]
    go_mod: str
    goarch: str
    goos: str
    bazel_version_file: str
    owner_map: dict[str, str]
    compilation_metadata: dict


# A concrete independent floor prevents a driver omission from disappearing
# from both the expected union and CodeQL's output. Generated paths retain
# their owning package suffix without depending on configuration-directory names.
REQUIRED_SOURCES = {
    "@@//external/tools/plugin-stack:backtrace_test": (
        "external/tools/plugin-stack/backtrace_test.go",
        "backtrace_test.internal.a.cgo/_cgo_gotypes.go",
        "backtrace_test.internal.a.cgo/_cgo_imports.go",
        "backtrace_test.internal.a.cgo/backtrace_unsafe.cgo1.go",
    ),
    "@@//pkg/segment/test:segment_test": ("pkg/segment/test/int_set.go", "pkg/segment/test/segment_test.go"),
    "@@//pkg/tcpip/header:header_test": ("pkg/tcpip/header/eth_test.go",),
    "@@//pkg/tcpip/header:header_x_test": ("pkg/tcpip/header/checksum_test.go",),
    "@@//pkg/tcpip/tests/integration:forward_test": ("pkg/tcpip/tests/integration/forward_test.go",),
    "@@//pkg/tcpip/tests/integration:iptables_test": ("pkg/tcpip/tests/integration/iptables_test.go",),
    "@@//test/runner:xml_test": ("test/runner/xml_test.go",),
    "@@//tools/go_generics/rules_tests:template_test": ("tools/go_generics/rules_tests/instance_test.go",),
    "@@//tools/licensecheck:licensecheck_test": ("tools/licensecheck/licensecheck_test.go",),
}


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
                if any(not isinstance(name, str) for name in record.get("CompiledGoFiles", [])):
                    raise ValueError(f"Non-string compiled source in {path}")
            case _:
                raise ValueError(f"Invalid package identity in {path}")
        records.append(cast(Package, record))
        text = text[end:]
    return records


def selected_packages(paths: list[str]) -> tuple[dict[str, Package], list[Package]]:
    """Keep real packages and account separately for their generated test mains."""
    selected: dict[str, Package] = {}
    testmains = []
    for name in paths:
        for record in json_records(Path(name)):
            if record["PkgPath"] == "testmain":
                testmains.append(record)
                continue
            identity = record["ID"]
            if identity in selected and selected[identity] != record:
                raise ValueError(f"Conflicting configured package records: {identity}")
            if not record.get("Dir"):
                raise ValueError(f"Missing declared package directory: {identity}")
            selected[identity] = record
    for record in testmains:
        if record["ID"] not in selected:
            raise ValueError(f"Synthetic testmain has no real test package: {record['ID']}")
    return selected, testmains


def compilation_records(manifest: Manifest, selected: dict[str, Package], resolve: Callable[[str], Path]) -> list[dict]:
    """Retain source/configuration facts for comparison with qualified evidence."""
    records = []

    def source_hashes(names: list[str]) -> dict[str, str]:
        hashes = {}
        for name in names:
            path = str(resolve(name).resolve())
            digest = hashlib.sha256(resolve(name).read_bytes()).hexdigest()
            if path in hashes and hashes[path] != digest:
                raise ValueError(f"Conflicting declared source contents: {path}")
            hashes[path] = digest
        return hashes

    for compilation, owner in manifest["owner_map"].items():
        current = manifest["compilation_metadata"][compilation]
        if current["owner"] != owner or any(current["runtime_attributes"].values()):
            raise ValueError(f"Compilation retains runtime attributes or loses its owner: {compilation}")
        roles = set()
        archives = []
        for raw in current["archives"]:
            archive = dict(raw)
            role = (archive["is_main"], archive["testfilter"])
            if role in roles:
                raise ValueError(f"Duplicate compiled archive role: {compilation} {role}")
            roles.add(role)
            sources = {field: source_hashes(archive.pop(field)) for field in ("srcs", "embedsrcs")}
            archives.append({"metadata": archive, "sources": sources})
        package = dict(selected[compilation])
        sources = {field: source_hashes(package.pop(field)) for field in ("GoFiles", "CompiledGoFiles", "OtherFiles")}
        records.append({"owner": owner, "compilation": compilation, "archives": archives, "package_metadata": package, "package_sources": sources})
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "codeql", "extractor", "driver", "output", "outcome", "go", "goroot"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    root = Path.cwd()
    output = args.output.absolute()
    output.mkdir(exist_ok=True)
    # This manifest is generated by package_probe.bzl in the same action graph.
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

    def resolve(name: str) -> Path:
        for placeholder in ("__BAZEL_WORKSPACE__", "__BAZEL_EXECROOT__", "__BAZEL_OUTPUT_BASE__"):
            name = name.replace(placeholder, str(root))
        return Path(name).absolute()

    selected, testmains = selected_packages(manifest["package_json"])
    if missing := set(manifest["labels"]) - selected.keys():
        raise ValueError(f"Missing requested package records: {sorted(missing)}")
    compilations = compilation_records(manifest, selected, resolve)

    raw_sources = set()
    for record in selected.values():
        if record["ID"].startswith("@@//"):
            record["Module"] = module
            raw_sources.update(str(resolve(name).resolve()) for name in record["CompiledGoFiles"])
    packages_path = output / "packages.json"
    packages_path.write_text("\n".join(json.dumps(record) for record in selected.values()) + "\n")
    stdlib_paths = [Path(name).absolute() for name in manifest["stdlib_json"]]
    for path in stdlib_paths:
        for record in json_records(path):
            if not record.get("Dir"):
                raise ValueError(f"Standard package lacks its source directory: {record['ID']}")
    driver_manifest = output / "driver.json"
    driver_manifest.write_text(json.dumps({
        "JSONFiles": [str(path) for path in stdlib_paths] + [str(packages_path)],
        "Labels": manifest["labels"],
        "BazelVersion": Path(manifest["bazel_version_file"]).read_text().strip(),
        "Workspace": str(root),
        "ExecRoot": str(root),
        "OutputBase": str(root),
        "Arch": manifest["goarch"],
        "Tests": True,
    }, indent=2) + "\n")
    (output / "synthetic-testmains.json").write_text(json.dumps(testmains, indent=2) + "\n")
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
        CODEQL_EXTRACTOR_GO_OPTION_EXTRACT_TESTS="true",
        # This probe checks declared Go inputs, not HTML in the toolchain tree.
        CODEQL_EXTRACTOR_GO_EXTRACT_HTML="no",
        GOTAGS="network_plugins",
    )
    # Reuse the driver's own architecture/tag filtering and test-package split.
    # The offline manifest returns its complete declared graph for every mode.
    response_path = output / "driver-response.json"
    with response_path.open("w") as response_file:
        subprocess.run(
            [str(args.driver.absolute()), *manifest["labels"]],
            input=json.dumps({"Mode": 0, "Tests": True}), text=True,
            env=environment, stdout=response_file, check=True,
        )
    match json.loads(response_path.read_text()):
        case {"Roots": list() as roots, "Packages": list() as packages}:
            if not all(isinstance(label, str) for label in roots):
                raise ValueError("Driver returned a non-string root identity")
        case _:
            raise ValueError("Driver did not return its declared package graph")
    if missing := set(manifest["labels"]) - set(roots):
        raise ValueError(f"Driver omitted requested roots: {sorted(missing)}")
    owner_map = manifest.get("owner_map", {})
    source_floor = dict(REQUIRED_SOURCES)
    if owner_map:
        source_floor["@@//test/rbe:triton_archive_integrity_test"] = (
            "test/rbe/source_archive_common_test.go", "test/rbe/source_archive_integrity_test.go",
        )
        if set(owner_map) != set(manifest["labels"]) or set(owner_map.values()) != source_floor.keys() or len(set(owner_map.values())) != len(owner_map):
            raise ValueError("Compilation owners differ from the independent fixture floor")
    elif set(manifest["labels"]) != source_floor.keys():
        raise ValueError("Requested roots differ from the independent fixture floor")
    eligible: dict[str, set[str]] = {}
    for record in packages:
        match record:
            case {"ID": str() as identity, "Dir": str() as directory} if directory:
                files = record.get("CompiledGoFiles", [])
                if not isinstance(files, list) or any(not isinstance(name, str) for name in files):
                    raise ValueError(f"Driver returned invalid source paths: {identity}")
            case _:
                raise ValueError("Driver lost package directory or source identity")
        if identity.startswith("@@//"):
            observed_module = record.get("Module")
            if (not isinstance(observed_module, dict)
                    or any(observed_module.get(key) != value for key, value in module.items())
                    or observed_module.get("Version")
                    or observed_module.get("Replace") is not None
                    or observed_module.get("Error") is not None):
                raise ValueError(f"Driver lost declared module provenance: {identity}")
            if identity in eligible:
                raise ValueError(f"Driver duplicated package identity: {identity}")
            eligible[identity] = {str(Path(name).resolve()) for name in files}
    floor = {}
    companions = {owner: label for label, owner in owner_map.items()}
    for identity, suffixes in source_floor.items():
        # MoveTestFiles may move a source into the canonical external-test clone.
        label = companions.get(identity, identity)
        files = eligible.get(label, set()) | eligible.get(label + "_xtest", set())
        floor[identity] = []
        for suffix in suffixes:
            if owner_map and ".internal.a.cgo/" in suffix:
                suffix = suffix.replace(identity.rsplit(":", 1)[1] + ".internal.a.cgo/", label.rsplit(":", 1)[1] + ".internal.a.cgo/", 1)
            match [name for name in files if name.endswith("/" + suffix)]:
                case [name]:
                    floor[identity].append(name)
                case matches:
                    raise ValueError(f"Required source {identity} {suffix}: expected one file, got {matches}")
    eligible_sources = set().union(*eligible.values())
    if introduced := eligible_sources - raw_sources:
        raise ValueError(f"Driver introduced undeclared source files: {sorted(introduced)}")
    expected = {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in eligible_sources}
    expected[str(go_mod.resolve())] = hashlib.sha256(go_mod.read_bytes()).hexdigest()
    (output / "expected-files.json").write_text(json.dumps(expected, indent=2) + "\n")
    # Embed data is a declared, readable compiler input, not an AST source file.
    embed = root / "tools/licensecheck/testdata/bsd.txt"
    embed_inputs = {str(embed.resolve()): hashlib.sha256(embed.read_bytes()).hexdigest()}
    database = output / "database"
    # CodeQL removes one leading @ from each @@ command argument. Escape the
    # canonical labels once so the strict driver receives their original bytes.
    command = [str(args.extractor.absolute()), "--source-root", str(root), *("@" + label for label in manifest["labels"])]
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
    missing = sorted(expected.keys() - actual.keys())
    mismatched = sorted(name for name in expected.keys() & actual.keys() if expected[name] != actual[name])
    unexpected = sorted(actual.keys() - expected.keys())
    outcome = {
        "requested_roots": manifest["labels"],
        "owner_map": owner_map,
        "compilations": compilations,
        "package_records": len(selected),
        "driver_roots": roots,
        "driver_package_records": len(packages),
        "goos": manifest["goos"],
        "goarch": manifest["goarch"],
        "tags": environment["GOTAGS"],
        "raw_to_eligible_exclusions": sorted(raw_sources - eligible_sources),
        "required_source_floor": floor,
        "embed_inputs": embed_inputs,
        "module": module,
        "expected_files": len(expected),
        "actual_files": len(actual),
        "missing": missing,
        "mismatched": mismatched,
        "unexpected": unexpected,
        "expected_sha256": expected,
        "actual_sha256": actual,
        "diagnostics": diagnostic_report,
        "extraction_errors": extraction_errors,
        "package_error_lines": package_error_lines,
    }
    args.outcome.write_text(json.dumps(outcome, indent=2) + "\n")
    print(json.dumps({key: value for key, value in outcome.items() if key not in {"expected_sha256", "actual_sha256", "diagnostics"}}), flush=True)
    if missing or mismatched or unexpected:
        raise ValueError("Extracted source coverage differs from declared package inputs")
    if extraction_errors or package_error_lines:
        raise ValueError("CodeQL reported incomplete or failed package extraction")


if __name__ == "__main__":
    main()
