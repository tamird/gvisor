#!/usr/bin/env python3
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

"""Select graph-declared architecture variants from canonical Bazel profiles."""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import re
import shlex
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class DispatchRow:
    """One existing qualifier selection and its coordinator requirements."""

    id: str
    lanes: list[str]
    architecture: str
    execution: str
    coordinator: str
    runner: str
    local_test_requirements: str
    targets: list[str]
    test_filter: str
    syscall_bucket: str
    work_seconds: int
    step_minutes: int
    job_minutes: int
    required_kinds: list[str]
    required_audits: list[str]


def coordinator_policy(lanes: list[str], architecture: str, execution: str,
                       requirements: str) -> tuple[str, str, int, int, int]:
    """Shared single/full dispatch policy; rule-owned deadlines are unchanged."""
    lane = " ".join(lanes)
    if execution == "remote":
        long = (architecture == "all" or lane in ("build", "test", "pgo")
                or (architecture == "amd64" and lane == "build-all")
                or (architecture == "arm64" and lane in ("syscalls-64k", "syscalls-rc")))
        # Former hosted-only rows retain their 45-minute work budget, with the
        # same 25-minute collection reserve used by long Actions coordinators.
        budget = (7200, 145, 155) if long else (2700, 70, 80)
        return "remote-actions", "ubuntu-24.04", *budget
    if execution != "local":
        raise ValueError(f"Unknown execution route: {execution}")
    if len(lanes) != 1:
        raise ValueError("Local execution requires exactly one lane")
    kvm = lane in ("container", "container-v1", "container-v1-pilot", "kvm",
                   "syscalls-kvm", "syscalls-rc-pilot", "syscalls-rc", "startup",
                   "posture", "benchmarks", "pgo") or "kvm" in requirements.split(",")
    runner = ("ubuntu-26.04" if (architecture == "amd64" or architecture == "all" and lane == "syscalls-rc") and kvm
              else "ubuntu-24.04-arm" if architecture == "arm64" or architecture == "all" and lane == "unit"
              else "ubuntu-24.04")
    if lane == "benchmarks":
        budget = (15000, 275, 285)
    elif lane == "pgo" or architecture == "all" and lane == "unit":
        budget = (7200, 145, 155)
    elif lane == "container-v1":
        budget = (8400, 150, 155)
    elif lane == "syscalls-rc":
        budget = (5400, 115, 120)
    elif lane == "container":
        budget = (4800, 90, 95)
    elif lane in ("syscalls", "syscalls-save", "syscalls-resume", "syscalls-kvm"):
        budget = (0, 90, 95)
    elif lane in ("cpu-images", "gpu-images", "language-goferfs"):
        budget = (0, 70, 75)
    elif lane == "container-v1-pilot":
        budget = (1200, 45, 50)
    elif lane in ("unit", "syscalls-rc-pilot", "docker", "plugin-network", "nftables", "moby", "kvm"):
        budget = (0, 45, 50)
    else:
        budget = (0, 15, 20)
    return "local", runner, *budget


def dispatch_kinds(lanes: list[str], architecture: str, execution: str) -> list[str]:
    """Require real work, not just a successful query or analysis preflight."""
    builds = {"build", "build-all", "presubmit-build", "plugin-build", "codeql",
              "lint-cc", "python-distributions", "website", "release-artifacts",
              "license-check", "license-fetch"}
    # Make runs the license tool directly after its captured Bazel builds;
    # the lane ledger records the tool's exit status.
    commands = {"gazelle"}
    if architecture == "all" and execution == "remote":
        # qualify.sh combines compatible lanes into one build or test command.
        separate = set(lanes) & {"presubmit-build", "lint-cc", "gazelle", "syscalls-rc"}
        combined = set(lanes) - separate
        kinds = ({"build" if combined <= builds else "test"} if combined else set())
        kinds.update("command" if lane in commands else "build" if lane in builds else "test" for lane in separate)
        return sorted(kinds)
    return sorted({"benchmark" if lane == "pgo" else "command" if lane in commands
                   else "build" if lane in builds else "test" for lane in lanes})


def dispatch_row(lanes: list[str], architecture: str, execution: str, *,
                 requirements: str = "namespace", targets: list[str] | None = None,
                 test_filter: str = "", syscall_bucket: str = "") -> DispatchRow:
    coordinator, runner, work, step, job = coordinator_policy(lanes, architecture, execution, requirements)
    selected = targets or []
    kinds = dispatch_kinds(lanes, architecture, execution)
    return DispatchRow(
        id="-".join([architecture, execution, *lanes]), lanes=lanes,
        architecture=architecture, execution=execution, coordinator=coordinator, runner=runner,
        local_test_requirements=requirements, targets=selected, test_filter=test_filter,
        syscall_bucket=syscall_bucket, work_seconds=work, step_minutes=step, job_minutes=job,
        required_kinds=kinds,
        # Configured declarations, including explicit empty image sets, own
        # applicability. A second lane-to-image list would silently go stale.
        required_audits=["source-images"] if set(kinds) - {"command"} else [],
    )


def public_dispatch_rows(pgo_manifest: Path) -> list[DispatchRow]:
    """Complete supported CPU routes, using the qualifier's existing selections."""
    rows = [dispatch_row(lanes.split(), arch, mode) for lanes, arch, mode in (
        ("presubmit-build", "all", "remote"),
        ("build-all", "amd64", "remote"),
        ("nogo workflows gazelle lint lint-cc governance", "all", "remote"),
        ("release-artifacts release-repository python-distributions website plugin-build go-export codeql", "all", "remote"),
        ("unit", "all", "local"),
        ("unit-v1 docker-v1", "all", "remote"),
        ("smoke smoke-race do bwrap", "all", "remote"),
        ("overlay swgso hostnet containerd fsstress packetimpact iptables nftables packetdrill kubernetes podman syzkaller", "all", "remote"),
        ("startup posture benchmarks", "all", "remote"),
        ("docker", "amd64", "remote"),
        ("syscalls-rc", "all", "local"),
        ("license-check", "amd64", "remote"),
        ("license-headers", "all", "remote"),
        ("cos-metadata", "all", "remote"),
    )]
    for lanes, arch, mode in (
        ("smoke bwrap docker cpu-images gpu-images", "arm64", "local"),
        ("container-v1 kvm startup posture portforward root plugin-network moby benchmarks", "amd64", "local"),
        ("syscalls syscalls-save syscalls-kvm", "amd64", "local"),
        ("syscalls syscalls-resume", "arm64", "local"),
        ("syscalls-64k kvm", "arm64", "remote"),
        ("language-directfs language-goferfs", "all", "remote"),
        ("language-goferfs", "amd64", "local"),
    ):
        rows.extend(dispatch_row([lane], arch, mode) for lane in lanes.split())
    # The public container-tests selection uses its declared KVM frontends.
    # container-v1-pilot is optional fork bring-up, not a public pipeline owner.
    rows.append(dispatch_row(["container"], "amd64", "local", requirements="kvm"))
    for image_class in ("cpu", "gpu"):
        row = dispatch_row(["test"], "all", "remote", targets=[f"//test/docker:{image_class}_image_sources_amd64_test"])
        rows.append(replace(row, id=f"amd64-remote-{image_class}-images"))
    pgo = json.loads(pgo_manifest.read_text())["pgo"]
    for platform, mode in (("systrap", "remote"), ("kvm", "local")):
        targets = sorted(label for label, declaration in pgo.items() if declaration["platform"] == platform)
        if not targets:
            raise ValueError(f"PGO manifest has no {platform} owners")
        rows.append(dispatch_row(["pgo"], "amd64", mode, targets=targets))
    return rows


def dispatch_plan(*, profile: str, source: str, run_id: str, run_attempt: int,
                  lanes: list[str], architecture: str, execution: str,
                  requirements: str, targets: list[str], test_filter: str,
                  syscall_bucket: str, header_base: str, cos_sha256: str,
                  cos_gzip_base64: str, clock_source: str, known_lanes: list[str],
                  pgo_manifest: Path) -> dict[str, object]:
    if (not re.fullmatch(r"[0-9a-f]{40}", source)
            or not re.fullmatch(r"[1-9][0-9]*", run_id)
            or type(run_attempt) is not int or run_attempt < 1):
        raise ValueError("Dispatch requires a full source SHA, positive decimal run ID and positive integer attempt")
    if clock_source not in ("calibrated", "reference") or architecture not in ("amd64", "arm64", "all"):
        raise ValueError("Invalid clock source or target architecture")
    if profile == "public-cpu":
        if targets or test_filter or syscall_bucket:
            raise ValueError("The public CPU profile cannot narrow targets, cases or syscall buckets")
        rows = public_dispatch_rows(pgo_manifest)
    elif profile == "single":
        rows = [dispatch_row(lanes, architecture, execution, requirements=requirements,
                             targets=targets, test_filter=test_filter, syscall_bucket=syscall_bucket)]
    else:
        raise ValueError(f"Unknown dispatch profile: {profile}")
    selected = {lane for row in rows for lane in row.lanes}
    if not selected or selected - set(known_lanes):
        raise ValueError(f"Unknown or empty qualification lanes: {sorted(selected - set(known_lanes))}")
    if "license-headers" in selected and not re.fullmatch(r"[0-9a-f]{40}", header_base):
        raise ValueError("License headers require the full comparison SHA")
    if "cos-metadata" in selected:
        if not re.fullmatch(r"[0-9a-f]{64}", cos_sha256) or not cos_gzip_base64:
            raise ValueError("COS metadata requires the complete encoded catalog and its SHA256")
        catalog = gzip.decompress(base64.b64decode(cos_gzip_base64, validate=True))
        if hashlib.sha256(catalog).hexdigest() != cos_sha256:
            raise ValueError("COS catalog SHA256 does not match its decoded bytes")
        if not isinstance(json.loads(catalog), list):
            raise ValueError("The complete COS catalog must be a JSON list")
    if len({row.id for row in rows}) != len(rows):
        raise ValueError("Dispatch row IDs must be unique")
    return {"schema_version": 1, "profile": profile, "source": source, "run_id": run_id,
            "run_attempt": run_attempt,
            "side_inputs": {"header_base": header_base, "cos_catalog_sha256": cos_sha256,
                            "clock_source": clock_source},
            "rows": [asdict(row) for row in rows]}


def dispatch_row_metadata(plan_path: Path, row_id: str, output: Path) -> bool:
    """Bind the running coordinator's inputs to its authenticated plan row."""
    plan_bytes = plan_path.read_bytes()
    plan = json.loads(plan_bytes)
    matches = [row for row in plan["rows"] if row["id"] == row_id]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one dispatch row: {row_id}")
    row = matches[0]
    expected = {
        "QUALIFICATION_COMMIT": plan["source"], "GITHUB_RUN_ID": plan["run_id"],
        "GITHUB_RUN_ATTEMPT": str(plan["run_attempt"]),
        "QUALIFICATION_LANES": " ".join(row["lanes"]),
        "QUALIFICATION_ARCH": row["architecture"], "QUALIFICATION_EXECUTION": row["coordinator"],
        "QUALIFICATION_TARGETS": " ".join(row["targets"]), "QUALIFICATION_TEST_FILTER": row["test_filter"],
        "QUALIFICATION_SYSCALL_BUCKET": row["syscall_bucket"],
        "QUALIFICATION_LOCAL_TEST_REQUIREMENTS": row["local_test_requirements"],
        "QUALIFICATION_HEADER_BASE": plan["side_inputs"]["header_base"],
        "QUALIFICATION_COS_SHA256": plan["side_inputs"]["cos_catalog_sha256"],
        "QUALIFICATION_CLOCK_SOURCE": plan["side_inputs"]["clock_source"],
    }
    for name, value in expected.items():
        if os.environ.get(name) != value:
            raise ValueError(f"Coordinator input {name} differs from dispatch plan")
    metadata = {key: plan[key] for key in ("schema_version", "source", "run_id", "run_attempt", "side_inputs")}
    metadata.update(plan_sha256=hashlib.sha256(plan_bytes).hexdigest(), row=row)
    output.write_text(json.dumps(metadata, indent=2) + "\n")
    return "source-images" in row["required_audits"]


def query_word(value: str) -> str:
    # Bazel query strings preserve backslashes; JSON escaping changes regexes.
    for quote in ('"', "'"):
        if quote not in value:
            return quote + value + quote
    raise ValueError(f"Bazel query cannot quote a word containing both quote types: {value}")


def target_set(labels: list[str]) -> str:
    return "set(" + " ".join(query_word(label) for label in labels) + ")"


def owner_labels(path: str, *, allow_empty: bool = False) -> list[str]:
    labels = Path(path).read_text().splitlines()
    if not labels and not allow_empty:
        raise ValueError(f"Expected nonempty canonical owner labels: {path}")
    if any(not label.startswith("//") for label in labels):
        raise ValueError(f"Expected canonical owner labels: {labels}")
    return sorted(set(labels))


def owner_query(patterns_path: str, unit_rc: str | None = None) -> str:
    patterns = [
        line.split("#", 1)[0].strip()
        for line in Path(patterns_path).read_text().splitlines()
    ]
    selection = target_set([])
    for pattern in patterns:
        if not pattern:
            continue
        exclude = pattern.startswith("-")
        operation = " except " if exclude else " union "
        selection = "(" + selection + operation + target_set([pattern[1:] if exclude else pattern]) + ")"
    # Generated variants and implementation targets are manual. Select only
    # original owners; final Bazel unit filters still govern their variants.
    manual = query_word(r"(^|\[|, )manual(,|\]|$)")
    result = (
        'attr(tags, "rbe-has-arm64-variant", ' + selection + ") except "
        + "attr(tags, " + manual + ", " + selection + ")"
    )
    if unit_rc is not None:
        # These are the same loading-time exclusions used by --config=unit.
        # Keep their declaration in .bazelrc, not in a second qualification list.
        filters: list[str] = []
        for line in Path(unit_rc).read_text().splitlines():
            words = shlex.split(line, comments=True)
            if words and words[0] in ("build:unit", "test:unit"):
                for option in words[1:]:
                    if option.startswith("--test_tag_filters="):
                        filters.append(option[len("--test_tag_filters="):])
        try:
            (value,) = filters
        except ValueError as error:
            raise ValueError(f"Expected one explicit unit test-tag filter in {unit_rc}: {filters}") from error
        if not value or not all(tag.startswith("-") and len(tag) > 1 for tag in value.split(",")):
            raise ValueError(f"Expected negative unit test-tag filters in {unit_rc}: {value}")
        excluded = "|".join(re.escape(tag[1:]) for tag in value.split(","))
        tags = query_word(r"(^|\[|, )(" + excluded + r")(,|\]|$)")
        result = "(" + result + ") except attr(tags, " + tags + ", " + selection + ")"
    return result


def unit_patterns(
    patterns_path: str,
    output_path: str,
    owners_path: str | None,
    environment_owners: list[str],
) -> None:
    """Retain canonical build roots and record each explicitly added owner."""
    variants = [owner + "_arm64" for owner in owner_labels(owners_path)] if owners_path else []
    patterns = Path(patterns_path).read_text().rstrip() + "\n"
    Path(output_path).write_text(patterns + "".join(label + "\n" for label in sorted(set(variants + environment_owners))))
    print(json.dumps({
        "canonical_patterns": patterns_path,
        "additional_arm64_variants": variants,
        "additional_environment_owners": environment_owners,
        "placement": "The declared test frontends apply local_test_requirements during the test invocation.",
        "limitation": "Loading selection only; configured owners, shards and placement come from the actual test results.",
    }, indent=2))


def test_requirements(
    actions_path: str,
    architecture: str | None = "arm64",
    configurations: dict[str, str] | None = None,
) -> dict[str, dict[str, str]]:
    decoded = json.loads(Path(actions_path).read_text())
    if (
        not isinstance(decoded, dict)
        or not isinstance(raw_targets := decoded.get("targets"), list)
        or not isinstance(actions := decoded.get("actions"), list)
    ):
        raise ValueError(f"Expected aquery targets and actions: {decoded}")
    targets: dict[str, str] = {}
    for target in raw_targets:
        if (
            not isinstance(target, dict)
            or not isinstance(target_id := target.get("id"), (str, int))
            or not isinstance(label := target.get("label"), str)
        ):
            raise ValueError(f"Invalid aquery target: {target}")
        targets[str(target_id)] = label
    checksums: dict[str, str] = {}
    if configurations is not None:
        if not isinstance(raw_configurations := decoded.get("configuration"), list):
            raise ValueError(f"Expected aquery configurations: {actions_path}")
        for configuration in raw_configurations:
            if (
                not isinstance(configuration, dict)
                or not isinstance(config_id := configuration.get("id"), (str, int))
                or not isinstance(checksum := configuration.get("checksum"), str)
            ):
                raise ValueError(f"Invalid action configuration: {configuration}")
            checksums[str(config_id)] = checksum
    requirements: dict[str, dict[str, str]] = {}
    architectures = ("amd64", "arm64") if architecture is None else (architecture,)
    for action in actions:
        if (
            not isinstance(action, dict)
            or action.get("mnemonic") != "TestRunner"
            or not isinstance(target_id := action.get("targetId"), (str, int))
            or not isinstance(entries := action.get("executionInfo"), list)
        ):
            raise ValueError(f"Expected a configured TestRunner: {action}")
        label = targets[str(target_id)]
        if configurations is not None:
            checksum = checksums[str(action["configurationId"])]
            if checksum != configurations.get(label):
                raise ValueError(f"Unexpected TestRunner configuration for {label}: {checksum}")
        properties: dict[str, str] = {}
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or not isinstance(key := entry.get("key"), str)
                or not isinstance(value := entry.get("value", ""), str)
            ):
                raise ValueError(f"Invalid execution property: {entry}")
            properties[key] = value
        if properties.get("Arch") not in architectures or properties.get("OSFamily") != "linux":
            raise ValueError(f"Unexpected native test routing for {label}: {properties}")
        if properties.get("workload-isolation-type") not in ("oci", "firecracker"):
            raise ValueError(f"Unknown worker requirement for {label}: {properties}")
        if label in requirements and requirements[label] != properties:
            raise ValueError(f"Conflicting shard requirements for {label}")
        requirements[label] = properties
    return requirements


@dataclass(frozen=True)
class ConfiguredTarget:
    tags: list[str]
    configuration: str
    aspects: tuple[str, ...] = ()


def configured_targets(
    events_path: str, *, tests: bool | None, allow_test_no_build: bool = False,
    expected_exit: int | None = None,
) -> dict[str, ConfiguredTarget]:
    """Read successful analysis roots; tests=None includes both target kinds."""
    targets: dict[str, ConfiguredTarget] = {}
    aspects: dict[tuple[str, str], list[str]] = {}
    skipped: set[tuple[str, str]] = set()
    no_build: set[tuple[str, str]] = set()
    all_configured: set[tuple[str, str]] = set()
    invalid_abort = False
    executed = False
    finish: dict[str, object] | None = None
    succeeded = False
    for line_number, line in enumerate(Path(events_path).read_text().splitlines(), start=1):
        event = json.loads(line)
        if not isinstance(event, dict) or not isinstance(event_id := event.get("id"), dict):
            raise ValueError(f"Invalid build event at {events_path}:{line_number}")
        if "buildFinished" in event_id:
            finished = event.get("finished")
            if not isinstance(finished, dict) or finish is not None:
                raise ValueError(f"Invalid or duplicate analysis finish: {event}")
            finish = finished
            succeeded = isinstance(finished, dict) and finished.get("overallSuccess") is True
        if "testResult" in event or "testSummary" in event:
            executed = True
        if isinstance(aborted := event.get("aborted"), dict):
            if aborted.get("reason") == "NO_BUILD":
                if not (
                    isinstance(completed := event_id.get("targetCompleted"), dict)
                    and isinstance(label := completed.get("label"), str)
                    and isinstance(configuration := completed.get("configuration"), dict)
                    and isinstance(checksum := configuration.get("id"), str)
                ):
                    raise ValueError(f"Invalid no-build identity: {event}")
                if not completed.get("aspect"):
                    no_build.add((label, checksum))
            elif aborted.get("reason") != "SKIPPED":
                invalid_abort = True
        if isinstance(aborted := event.get("aborted"), dict) and aborted.get("reason") == "SKIPPED":
            if not (
                isinstance(completed := event_id.get("targetCompleted"), dict)
                and isinstance(label := completed.get("label"), str)
                and isinstance(configuration := completed.get("configuration"), dict)
                and isinstance(checksum := configuration.get("id"), str)
            ):
                raise ValueError(f"Invalid skipped target identity: {event}")
            if not completed.get("aspect"):
                skipped.add((label, checksum))
        if "targetConfigured" not in event_id:
            continue
        identity = event_id["targetConfigured"]
        configured = event.get("configured")
        children = event.get("children")
        if (
            not isinstance(identity, dict)
            or not isinstance(label := identity.get("label"), str)
            or not isinstance(configured, dict)
            or not isinstance(tags := configured.get("tag", []), list)
            or any(not isinstance(tag, str) for tag in tags)
            or not isinstance(children, list)
        ):
            raise ValueError(f"Invalid configured target: {event}")
        if configured.get("targetKind") == "test_suite rule":
            continue
        configurations = []
        for child in children:
            if (
                isinstance(child, dict)
                and isinstance(completed := child.get("targetCompleted"), dict)
                and completed.get("label") == label
                and completed.get("aspect", "") == identity.get("aspect", "")
                and isinstance(configuration := completed.get("configuration"), dict)
                and isinstance(checksum := configuration.get("id"), str)
            ):
                configurations.append(checksum)
        if len(configurations) != 1:
            raise ValueError(f"Ambiguous top-level target identity: {event}")
        # An aspect is a separate build obligation, not another selected root.
        # Retain its name for the reporter's independent expected selection.
        if aspect := identity.get("aspect"):
            names = aspects.setdefault((label, configurations[0]), [])
            if not isinstance(aspect, str) or aspect in names:
                raise ValueError(f"Invalid or duplicate configured aspect: {event}")
            names.append(aspect)
            continue
        if label in targets:
            raise ValueError(f"Ambiguous top-level target identity: {event}")
        all_configured.add((label, configurations[0]))
        if tests is not None and ("testSize" in configured) != tests:
            continue
        targets[label] = ConfiguredTarget(tags, configurations[0])
    if orphaned := aspects.keys() - all_configured:
        raise ValueError(f"Configured aspects have no matching ordinary root: {sorted(orphaned)}")
    for label, checksum in sorted(skipped):
        if (target := targets.get(label)) is not None and target.configuration == checksum:
            del targets[label]
            print(f"Bazel skipped {label} in configuration {checksum}", file=sys.stderr)
    # Bazel 8's test --nobuild finishes with BUILD_FAILURE/1 even after
    # successful analysis. Accept only its complete NO_BUILD event pattern,
    # never a generic nonzero exit or a target-pattern/analysis failure.
    finish_exit = finish.get("exitCode", {}) if finish is not None else {}
    if not isinstance(finish_exit, dict):
        raise ValueError(f"Invalid analysis exit: {events_path}")
    if expected_exit is not None and finish_exit.get("code", 0) != expected_exit:
        raise ValueError(f"Native exit differs from analysis finish: {events_path}")
    if expected_exit is not None and (invalid_abort or executed):
        raise ValueError(f"Preflight contains a failure or test execution: {events_path}")
    if allow_test_no_build and tests is True and not succeeded:
        succeeded = (
            finish_exit == {"name": "BUILD_FAILURE", "code": 1}
            and bool(all_configured - skipped)
            and all_configured - skipped == no_build
            and not invalid_abort
            and not executed
        )
    if not succeeded or not targets:
        raise ValueError(f"Expected successful, nonempty target analysis: {events_path}")
    return {label: ConfiguredTarget(target.tags, target.configuration,
                                    tuple(aspects.get((label, target.configuration), ())))
            for label, target in targets.items()}


def configured_tests(events_path: str) -> dict[str, ConfiguredTarget]:
    """Read this successful analysis invocation's runnable top-level tests."""
    return configured_targets(events_path, tests=True)


def cgroup_targets(events_path: str, *, guest_kvm: bool = False) -> list[str]:
    targets = configured_tests(events_path)
    missing = sorted(label for label, target in targets.items() if "rbe-has-cgroup-v1-variant" not in target.tags)
    if missing:
        raise ValueError(f"Canonical owners without cgroup-v1 variants: {missing}")
    selected = []
    for label, target in targets.items():
        if guest_kvm and "requires-kvm" in target.tags:
            if "rbe-has-cgroup-v1-guest-variant" not in target.tags:
                raise ValueError(f"KVM owner without a declared cgroup-v1 guest: {label}")
            selected.append(label + "_cgroup_v1_guest")
        else:
            selected.append(label + "_cgroup_v1")
    return sorted(selected)


def container_platform_targets(events_path: str) -> list[str]:
    """Expand only owners that advertise separately declared runtime variants."""
    prefix, suffix = "rbe-has-container-", "-variant"
    selected = []
    for label, target in configured_tests(events_path).items():
        platforms = [
            tag[len(prefix):-len(suffix)]
            for tag in target.tags
            if tag.startswith(prefix) and tag.endswith(suffix)
        ]
        if platforms:
            selected.extend(label + "_" + platform for platform in platforms)
        else:
            selected.append(label)
    return sorted(selected)


def profile_suffix(
    architecture: str, page_size: str, *, hybrid: bool = False, rc_kernel: bool = False,
) -> str:
    if rc_kernel:
        if page_size != "4k" or hybrid:
            raise ValueError("RC guests require ordinary 4K payloads without host-native routing")
        return "_rc_kvm" if architecture == "amd64" else "_rc_tcg"
    if page_size == "64k":
        if architecture != "arm64":
            raise ValueError("The public 64K syscall profile requires ARM64")
        return "_64k_tcg"
    return "_" + architecture if architecture == "arm64" or hybrid else ""


def profile_targets(
    events_path: str, architecture: str, page_size: str = "4k", *, hybrid: bool = False,
    kvm_only: bool = False, rc_kernel: bool = False,
) -> list[str]:
    targets = configured_tests(events_path)
    if kvm_only and (not hybrid or architecture != "amd64"):
        raise ValueError("The KVM-only profile requires hybrid AMD64 execution")
    if rc_kernel:
        # Nogo retains its own lane; only syscall payloads run in RC guests.
        targets = {label: target for label, target in targets.items() if "nogo" not in target.tags}
    if hybrid:
        # Filter before requiring native variants: Nogo owns its separate lane,
        # and the KVM lane is independent of ordinary namespace execution.
        targets = {
            label: target for label, target in targets.items()
            if ("runsc_kvm" in target.tags) == kvm_only and "nogo" not in target.tags
        }
        if not targets:
            raise ValueError("The public profile contains no eligible syscall owners")
    suffix = profile_suffix(architecture, page_size, hybrid=hybrid, rc_kernel=rc_kernel)
    if not suffix:
        return sorted(targets)
    tag = "rbe-has" + suffix.replace("_", "-") + "-variant"
    missing = sorted(label for label, target in targets.items() if tag not in target.tags)
    if missing:
        raise ValueError(f"Profile owners without {suffix} variants: {missing}")
    return sorted(label + suffix for label in targets)


def hybrid_local_owners(
    selected: set[str], requirements: dict[str, dict[str, str]], *, kvm: bool = False,
) -> dict[str, list[str]]:
    """Check the same graph-owned locality and identity contract for each lane."""
    groups: dict[str, list[str]] = {"root": [], "unprivileged": []}
    for label in sorted(selected):
        properties = requirements[label]
        local = bool({"no-remote-exec", "no-remote"}.intersection(properties))
        if not local:
            if kvm:
                raise ValueError(f"KVM frontend is not local: {label}")
            continue
        if "no-local" in properties:
            raise ValueError(f"Local frontend forbids local execution: {label}")
        if not kvm and properties["workload-isolation-type"] != "firecracker":
            raise ValueError(f"Unexpected local namespace fixture: {label}")
        user = "root" if kvm else properties.get("dockerUser")
        if user == "root":
            groups["root"].append(label)
        elif user == "nobody":
            groups["unprivileged"].append(label)
        else:
            raise ValueError(f"Unsupported local test identity for {label}: {properties}")
    return groups


def local_routing_reasons(
    groups: dict[str, list[str]], requirements: dict[str, dict[str, str]],
    configured: dict[str, ConfiguredTarget], missing: str,
) -> dict[str, list[str]]:
    """Explain actual local actions from the caller policy and owning tags."""
    unavailable = set(missing.split(",")) if missing else set()
    reasons: dict[str, list[str]] = {}
    for labels in groups.values():
        for label in labels:
            tags = set(configured[label].tags)
            if "runsc_kvm" in tags:
                reasons[label] = ["kvm"]
            elif {"native", "requires-initial-cgroup-namespace"} <= tags:
                reasons[label] = ["initial-cgroup-namespace"]
            else:
                needed = {tag[len("rbe-requires-host:"):] for tag in tags if tag.startswith("rbe-requires-host:")}
                if requirements[label]["workload-isolation-type"] == "firecracker":
                    needed.add("namespace")
                reasons[label] = sorted(needed & unavailable)
                if not reasons[label]:
                    raise ValueError(f"Local frontend has no matching missing requirement: {label}: {missing}")
    return reasons


def syscall_buckets(value: str) -> tuple[int, ...]:
    """Parse a canonical set so workflow concurrency keys have one spelling."""
    if not re.fullmatch(r"(?:[0-9]|1[0-4])(?:,(?:[0-9]|1[0-4]))*", value):
        raise argparse.ArgumentTypeError("Expected comma-separated syscall buckets from 0 through 14")
    buckets = tuple(map(int, value.split(",")))
    if buckets != tuple(sorted(set(buckets))):
        raise argparse.ArgumentTypeError("Syscall buckets must be distinct and in increasing order")
    return buckets


def select_profile(
    profile_path: str,
    architecture: str,
    events_path: str,
    actions_path: str,
    output_path: str,
    *,
    syscall_policy: bool,
    page_size: str = "4k",
    hybrid: bool = False,
    kvm_only: bool = False,
    rc_kernel: bool = False,
    selected_buckets: tuple[int, ...] | None = None,
    selected_owners: list[str] | None = None,
    mixed_rc_owners: bool = False,
    local_test_requirements: str = "namespace",
) -> None:
    if hybrid and (not syscall_policy or page_size != "4k"):
        raise ValueError("Hybrid syscall execution requires a native 4K syscall profile")
    guest = page_size == "64k" or rc_kernel
    if selected_buckets is not None and not (hybrid or syscall_policy and guest):
        raise ValueError("Syscall buckets require hybrid or guest execution")
    if mixed_rc_owners and not rc_kernel:
        raise ValueError("Mixed architecture owners require RC profiles")
    if selected_owners is not None:
        if not syscall_policy or not (hybrid or rc_kernel):
            raise ValueError("Explicit syscall owners require a hybrid or RC profile")
        if selected_buckets is not None:
            raise ValueError("Select explicit syscall owners or buckets, not both")
        if not selected_owners or len(selected_owners) != len(set(selected_owners)):
            raise ValueError("Explicit syscall owners must be nonempty and distinct")
    original = configured_tests(profile_path)
    expected = set(profile_targets(profile_path, architecture, page_size, hybrid=hybrid, kvm_only=kvm_only, rc_kernel=rc_kernel))
    configured = configured_tests(events_path)
    if configured.keys() != expected:
        raise ValueError(f"Configured owners differ from profile: {sorted(configured.keys() ^ expected)}")
    execution_architecture = "amd64" if guest else architecture
    requirements = test_requirements(
        actions_path,
        execution_architecture,
        {label: target.configuration for label, target in configured.items()},
    )
    if requirements.keys() != expected:
        raise ValueError(f"Missing profile TestRunners: {sorted(expected - requirements.keys())}")
    if guest:
        for label, target in configured.items():
            properties = requirements[label]
            if rc_kernel and architecture == "amd64":
                required_tags = {"amd64-rc-kvm", "no-remote-exec", "no-sandbox"}
                platforms = {"native", "runsc_ptrace", "runsc_systrap", "runsc_kvm"}
                if not {"no-remote-exec", "no-sandbox"} <= properties.keys() or "no-local" in properties:
                    raise ValueError(f"Expected local KVM guest: {label}: {properties}")
            else:
                required_tags = {"arm64-rc-tcg" if rc_kernel else "arm64-64k-tcg", "no-local"}
                platforms = {"runsc_ptrace", "runsc_systrap"} if rc_kernel else {"runsc_systrap"}
                if (
                    properties.get("dockerUser") != "nobody"
                    or properties["workload-isolation-type"] != "oci"
                    or "no-local" not in properties
                    or {"no-remote-exec", "no-remote"}.intersection(properties)
                ):
                    raise ValueError(f"Expected unprivileged TCG worker: {label}: {properties}")
            if not required_tags <= set(target.tags) or not platforms.intersection(target.tags) or "allsave" in target.tags:
                raise ValueError(f"Unexpected guest variant: {label}: {target.tags}")
    unavailable: dict[str, str] = {}
    policy_excluded: dict[str, str] = {}
    for label, target in original.items():
        if kvm_only and "runsc_kvm" not in target.tags:
            policy_excluded[label] = "Outside the local KVM-only syscall lane; not executed by this invocation."
            continue
        variant = label + profile_suffix(architecture, page_size, hybrid=hybrid, rc_kernel=rc_kernel)
        # The standalone RBE syscall lane also leaves Nogo to its own lane.
        # Keep this policy distinct from unavailable runtime capabilities.
        if syscall_policy and "nogo" in target.tags:
            policy_excluded[label if hybrid or rc_kernel else variant] = "Nogo runs in the dedicated nogo lane."
        elif syscall_policy and "runsc_kvm" in target.tags and not kvm_only and not rc_kernel:
            if hybrid:
                policy_excluded[label] = "KVM runs in the dedicated syscalls-kvm lane."
            else:
                unavailable[variant] = "KVM execution is unavailable."
        elif not hybrid and architecture == "arm64" and requirements[variant]["workload-isolation-type"] == "firecracker":
            unavailable[variant] = "ARM64 Firecracker execution is unavailable."
    eligible = expected - unavailable.keys() - policy_excluded.keys()
    buckets: dict[str, list[str]] = {}
    if selected_buckets is not None:
        buckets = {str(bucket): [] for bucket in range(15)}
        for label in sorted(eligible):
            tags = [tag[len("hash15:"):] for tag in configured[label].tags if tag.startswith("hash15:")]
            if len(tags) != 1 or tags[0] not in buckets:
                raise ValueError(f"Expected one graph-declared hash15 bucket for {label}: {tags}")
            buckets[tags[0]].append(label)
        for bucket in selected_buckets:
            if not buckets[str(bucket)]:
                raise ValueError(f"The canonical profile has no owners in syscall bucket {bucket}")
        selected = sorted(label for bucket in selected_buckets for label in buckets[str(bucket)])
    else:
        selected = sorted(eligible)
    unexecuted_bucket_owners = sorted(eligible - set(selected))
    if selected_owners is not None:
        if mixed_rc_owners:
            # Both complete routing graphs are checked independently. The
            # combined selection must account for every requested owner.
            selected_owners = [owner for owner in selected_owners if owner in eligible]
        if unknown := set(selected_owners) - eligible:
            raise ValueError(f"Explicit owners outside the eligible syscall profile: {sorted(unknown)}")
        selected = sorted(selected_owners)
    groups = hybrid_local_owners(set(selected), requirements, kvm=kvm_only) if hybrid else {}
    if rc_kernel and architecture == "amd64":
        groups = {"root": selected, "unprivileged": []}
    local = {label for group in groups.values() for label in group}
    initial_cgroup = sorted(
        label for label in local
        if not rc_kernel and {"native", "requires-initial-cgroup-namespace"} <= set(configured[label].tags)
    )
    for label in initial_cgroup:
        if "no-sandbox" not in requirements[label]:
            raise ValueError(f"Initial cgroup namespace owner lacks no-sandbox: {label}")
    Path(output_path).write_text("".join(label + "\n" for label in selected))
    print(json.dumps({
        "profile_architecture": architecture,
        "profile_page_size": page_size,
        **({"profile_kernel": "rc"} if rc_kernel else {}),
        "canonical_profile": profile_path,
        "canonical_owners": sorted(original),
        "selected_owners": selected,
        "explicit_owners": selected_owners,
        "unexecuted_owners": sorted(eligible - set(selected)),
        "unavailable_owners": unavailable,
        "policy_excluded_owners": policy_excluded,
        "local_owners": groups,
        "initial_cgroup_owners": initial_cgroup,
        "local_requirements": {label: requirements[label] for label in sorted(local)},
        "local_test_requirements": local_test_requirements.split(",") if hybrid and local_test_requirements else [],
        "local_routing_reasons": local_routing_reasons(groups, requirements, configured, local_test_requirements) if hybrid else {},
        "remote_owners": sorted(set(selected) - local),
        "syscall_bucket": selected_buckets[0] if selected_buckets and len(selected_buckets) == 1 else None,
        "selected_syscall_buckets": selected_buckets,
        "syscall_buckets": buckets,
        "unexecuted_bucket_owners": unexecuted_bucket_owners,
    }, indent=2))


def combine_rc_profiles(selection_dir: str, selected_owners: list[str] | None = None) -> None:
    root = Path(selection_dir)
    profiles = {
        arch: json.loads((root / f"{arch}-selection.json").read_text())
        for arch in ("amd64", "arm64")
    }
    selected = [label for profile in profiles.values() for label in profile["selected_owners"]]
    if len(selected) != len(set(selected)):
        raise ValueError("RC architecture selections overlap")
    if selected_owners is not None:
        if not selected_owners or len(selected_owners) != len(set(selected_owners)):
            raise ValueError("Explicit syscall owners must be nonempty and distinct")
        if set(selected) != set(selected_owners):
            raise ValueError(f"Combined RC profile changes explicit owners: {sorted(set(selected) ^ set(selected_owners))}")
    print(json.dumps({
        "profile_architecture": "all",
        "profile_kernel": "rc",
        "profiles": profiles,
        "selected_owners": sorted(selected),
        "explicit_owners": selected_owners,
        "unexecuted_owners": sorted(label for profile in profiles.values() for label in profile["unexecuted_owners"]),
    }, indent=2))


def universe(patterns_path: str) -> str:
    patterns = [line.split("#", 1)[0].strip() for line in Path(patterns_path).read_text().splitlines()]
    if any("," in pattern for pattern in patterns):
        raise ValueError(f"Aquery universe cannot represent comma-containing patterns: {patterns_path}")
    return ",".join(pattern for pattern in patterns if pattern)


def select_variants(
    patterns_path: str,
    owners_path: str,
    actions_path: str,
    output_path: str,
    profile_path: str | None,
    *,
    environment_owners: list[str],
    hybrid: bool = False,
    local_test_requirements: str = "namespace",
) -> None:
    owners = owner_labels(owners_path)
    variants = {owner + "_arm64" for owner in owners}
    environment = set(environment_owners)
    expected = variants | environment
    requirements = test_requirements(actions_path, architecture="arm64" if hybrid else None)
    if requirements.keys() != expected:
        raise ValueError(f"Configured test owners differ from selection: {sorted(expected ^ requirements.keys())}")
    for owner, properties in requirements.items():
        architecture = "arm64" if owner in variants else owner.rsplit("_", 1)[-1]
        if properties["Arch"] != architecture:
            raise ValueError(f"Unexpected target architecture for {owner}: {properties['Arch']}")
    if hybrid:
        if profile_path is None:
            raise ValueError("Hybrid units require the canonical native unit profile")
        original = configured_tests(profile_path)
        if missing := environment - original.keys():
            raise ValueError(f"Environment owners missing from configured unit profile: {sorted(missing)}")
        eligible = (variants & {label + "_arm64" for label in original}) | environment
        groups = hybrid_local_owners(eligible, requirements)
        selected = sorted(label for group in groups.values() for label in group)
        remote = sorted(eligible - set(selected))
        shared = sorted(set(original) - set(owners) - environment)
        targets = sorted(eligible | set(shared))
        Path(output_path).write_text("".join(label + "\n" for label in targets))
        print(json.dumps({
            "canonical_selection": profile_path,
            "selected_owners": targets,
            "additional_environment_owners": environment_owners,
            "local_owners": groups,
            "local_requirements": {label: requirements[label] for label in selected},
            "local_test_requirements": local_test_requirements.split(",") if local_test_requirements else [],
            "local_routing_reasons": local_routing_reasons(
                groups, requirements,
                {label: original[label if label in environment else label[:-len("_arm64")]] for label in eligible},
                local_test_requirements,
            ),
            "remote_arm64_owners": remote,
            "shared_owners": shared,
            "profile_excluded_variants": sorted(expected - eligible),
        }, indent=2))
        return
    unavailable = sorted(
        label for label, properties in requirements.items()
        if properties["Arch"] == "arm64" and properties["workload-isolation-type"] == "firecracker"
    )
    available = expected - set(unavailable)
    selected = sorted(variants & available)
    selected_environment = sorted(environment & available)
    if profile_path is None:
        # Ordinary mixed units retain their original patterns and build-only
        # work. Other lanes that require neutral filters use canonical leaves.
        output = Path(patterns_path).read_text().rstrip() + "\n" + "\n".join(selected) + "\n"
    else:
        original = configured_tests(profile_path)
        selected = sorted(set(selected) & {label + "_arm64" for label in original})
        output = "".join(label + "\n" for label in sorted(original) + selected)
    Path(output_path).write_text(output + "".join(label + "\n" for label in selected_environment))
    print(json.dumps({
        "additional_arm64_variants": selected,
        "additional_environment_owners": selected_environment,
        "unavailable_arm64_firecracker": unavailable,
        "canonical_selection": profile_path or patterns_path,
    }, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    dispatch = commands.add_parser("dispatch", help="Plan the single or complete public CPU workflow")
    dispatch.add_argument("--known-lanes", nargs="+", required=True)
    dispatch.add_argument("--pgo-manifest", type=Path, default=Path("test/benchmarks/pgo.json"))
    row_metadata = commands.add_parser("dispatch-row")
    row_metadata.add_argument("--plan", type=Path, required=True)
    row_metadata.add_argument("--id", required=True)
    row_metadata.add_argument("--output", type=Path, required=True)
    lane_result = commands.add_parser("lane-result", help="Retain a completed qualifier call's exact lane group")
    lane_result.add_argument("--output", type=Path, required=True)
    lane_result.add_argument("--native-exit", type=int, required=True)
    lane_result.add_argument("lanes", nargs="+")
    policy = commands.add_parser("coordinator-policy")
    policy.add_argument("--architecture", required=True)
    policy.add_argument("--execution", choices=("local", "remote"), required=True)
    policy.add_argument("--requirements", default="namespace")
    policy.add_argument("lanes", nargs="+")
    buckets = commands.add_parser("syscall-buckets")
    buckets.add_argument("buckets", type=syscall_buckets)
    query = commands.add_parser("query")
    query.add_argument("patterns")
    query.add_argument("--unit-rc", help="Apply the canonical unit test-tag exclusions before adding ARM variants")
    unit = commands.add_parser("unit-patterns")
    for name in ("patterns", "output"):
        unit.add_argument(name)
    unit.add_argument("--owners", help="Add ARM64 variants of these canonical owners")
    unit.add_argument("--environment-owners", help="Add these exact complementary test frontends")
    actions = commands.add_parser("actions")
    actions.add_argument("owners")
    actions.add_argument("--exact", action="store_true", help="Query these configured labels without adding ARM64 variants")
    actions.add_argument("--additional-owners", help="Include these exact configured labels in the same query")
    select = commands.add_parser("select")
    for name in ("patterns", "owners", "actions", "output"):
        select.add_argument(name)
    select.add_argument("--profile", help="Select explicit ordinary and ARM owners from this canonical unit profile")
    select.add_argument("--hybrid", action="store_true", help="Select the canonical ARM profile with namespace owners local and ordinary/shared owners remote")
    select.add_argument("--local-test-requirements", default="namespace", help="Comma-separated requirements missing from remote workers")
    select.add_argument("--environment-owners", help="Add these exact complementary test frontends")
    cgroup = commands.add_parser("cgroup-targets")
    cgroup.add_argument("events")
    cgroup.add_argument("--guest-kvm", action="store_true")
    container = commands.add_parser("container-targets")
    container.add_argument("events")
    container.add_argument("--architecture", choices=("amd64", "arm64"))
    container_platforms = commands.add_parser("container-platform-targets")
    container_platforms.add_argument("events")
    roots = commands.add_parser("build-roots")
    roots.add_argument("events")
    patterns = commands.add_parser("universe-rc")
    patterns.add_argument("patterns")
    kvm = commands.add_parser("kvm-query")
    kvm.add_argument("roots")
    filtered = commands.add_parser("select-filtered")
    for name in ("events", "kvm_owners", "output"):
        filtered.add_argument(name)
    for command in ("profile-actions", "profile-targets"):
        profile = commands.add_parser(command)
        profile.add_argument("events")
        profile.add_argument("architecture", choices=("amd64", "arm64"))
        profile.add_argument("--page-size", choices=("4k", "64k"), default="4k")
        profile.add_argument("--rc-kernel", action="store_true", help="Select the declared RC guest for this architecture")
        profile.add_argument("--hybrid", action="store_true", help="Select native local variants")
        profile.add_argument("--kvm-only", action="store_true", help="Select only KVM syscall owners")
    profile = commands.add_parser("select-profile")
    profile.add_argument("profile")
    profile.add_argument("architecture", choices=("amd64", "arm64"))
    profile.add_argument("--page-size", choices=("4k", "64k"), default="4k")
    profile.add_argument("--rc-kernel", action="store_true", help="Select the declared RC guest for this architecture")
    for name in ("events", "actions", "output"):
        profile.add_argument(name)
    profile.add_argument("--syscall-policy", action="store_true", help="Apply the existing syscall runtime exclusions")
    profile.add_argument("--hybrid", action="store_true", help="Run graph-declared native namespace owners locally")
    profile.add_argument("--kvm-only", action="store_true", help="Select only KVM syscall owners")
    profile.add_argument("--syscall-bucket", type=syscall_buckets, help="Select increasing comma-separated hash15 buckets and report the full partition")
    profile.add_argument("--owner", action="append", help="Select an exact eligible syscall owner and report the unexecuted complement")
    profile.add_argument("--mixed-rc-owners", action="store_true", help="Intersect owners with this RC profile; require combine-rc-profiles to verify the full selection")
    profile.add_argument("--local-test-requirements", default="namespace", help="Comma-separated requirements missing from remote workers")
    combined = commands.add_parser("combine-rc-profiles")
    combined.add_argument("directory")
    combined.add_argument("--owner", action="append", help="Require exactly these owners across both RC architectures")
    verify = commands.add_parser("verify")
    verify.add_argument("targets")
    verify.add_argument("events")
    verify.add_argument("--profile", action="append", help="Successful analysis of additional suite roots")
    args = parser.parse_args()
    if args.command == "dispatch":
        environment = os.environ
        plan = dispatch_plan(
            profile=environment.get("QUALIFICATION_PROFILE", "single"),
            source=environment["QUALIFICATION_COMMIT"], run_id=environment["GITHUB_RUN_ID"],
            run_attempt=int(environment["GITHUB_RUN_ATTEMPT"]),
            lanes=environment.get("QUALIFICATION_LANES", "smoke").split(),
            architecture=environment.get("QUALIFICATION_ARCH", "amd64"),
            execution=environment.get("QUALIFICATION_EXECUTION", "remote"),
            requirements=environment.get("QUALIFICATION_LOCAL_TEST_REQUIREMENTS", "namespace"),
            targets=environment.get("QUALIFICATION_TARGETS", "").split(),
            test_filter=environment.get("QUALIFICATION_TEST_FILTER", ""),
            syscall_bucket=environment.get("QUALIFICATION_SYSCALL_BUCKET", ""),
            header_base=environment.get("QUALIFICATION_HEADER_BASE", ""),
            cos_sha256=environment.get("QUALIFICATION_COS_SHA256", ""),
            cos_gzip_base64=environment.get("QUALIFICATION_COS_GZIP_BASE64", ""),
            clock_source=environment.get("QUALIFICATION_CLOCK_SOURCE", "calibrated"),
            known_lanes=args.known_lanes, pgo_manifest=args.pgo_manifest,
        )
        print(json.dumps(plan, separators=(",", ":")))
    elif args.command == "dispatch-row":
        print("true" if dispatch_row_metadata(args.plan, args.id, args.output) else "false")
    elif args.command == "lane-result":
        with args.output.open("a") as output:
            output.write(json.dumps({"lanes": args.lanes, "native_exit": args.native_exit}) + "\n")
    elif args.command == "coordinator-policy":
        print(coordinator_policy(args.lanes, args.architecture, args.execution, args.requirements)[2])
    elif args.command == "syscall-buckets":
        print(",".join(map(str, args.buckets)))
    elif args.command == "query":
        print(owner_query(args.patterns, args.unit_rc))
    elif args.command == "unit-patterns":
        environment = owner_labels(args.environment_owners) if args.environment_owners else []
        unit_patterns(args.patterns, args.output, args.owners, environment)
    elif args.command == "actions":
        suffix = "" if args.exact else "_arm64"
        labels = [owner + suffix for owner in owner_labels(args.owners)]
        if args.additional_owners:
            labels.extend(owner_labels(args.additional_owners))
        print('mnemonic("^TestRunner$", ' + target_set(sorted(set(labels))) + ")")
    elif args.command == "select":
        environment = owner_labels(args.environment_owners) if args.environment_owners else []
        select_variants(args.patterns, args.owners, args.actions, args.output, args.profile, hybrid=args.hybrid, local_test_requirements=args.local_test_requirements, environment_owners=environment)
    elif args.command == "cgroup-targets":
        print("\n".join(cgroup_targets(args.events, guest_kvm=args.guest_kvm)))
    elif args.command == "container-platform-targets":
        print("\n".join(container_platform_targets(args.events)))
    elif args.command == "container-targets":
        targets = configured_tests(args.events)
        if args.architecture:
            missing = sorted(
                label for label, target in targets.items()
                if f"rbe-has-{args.architecture}-variant" not in target.tags
            )
            if missing:
                raise ValueError(f"Container owners without {args.architecture} variants: {missing}")
        print("\n".join(sorted(
            (label + "_cgroup_v2" if "rbe-has-cgroup-v2-variant" in target.tags else label)
            + ("_" + args.architecture if args.architecture else "")
            for label, target in targets.items()
        )))
    elif args.command == "build-roots":
        print("\n".join(sorted(configured_targets(args.events, tests=False))))
    elif args.command == "universe-rc":
        # Bazel's rc tokenizer consumes backslash escapes inside double quotes.
        # https://github.com/bazelbuild/bazel/blob/d84820503/src/main/cpp/util/strings.cc#L181-L220
        value = universe(args.patterns).replace("\\", "\\\\").replace('"', '\\"')
        print('aquery:rbe-selection "--universe_scope=' + value + '"')
    elif args.command == "kvm-query":
        tag = query_word(r"(^|\[|, )requires-kvm(,|\]|$)")
        print("attr(tags, " + tag + ", tests(" + target_set(owner_labels(args.roots)) + "))")
    elif args.command == "select-filtered":
        selected = configured_tests(args.events)
        kvm_owners = owner_labels(args.kvm_owners, allow_empty=True)
        if overlap := selected.keys() & set(kvm_owners):
            raise ValueError(f"KVM profile filter retained excluded owners: {sorted(overlap)}")
        Path(args.output).write_text("".join(label + "\n" for label in sorted(selected)))
        print(json.dumps({
            "selected_filtered_owners": sorted(selected),
            "excluded_kvm_owners": kvm_owners,
            "limitation": "KVM identities come from loading only; their configurations and execution remain unqualified.",
        }, indent=2))
    elif args.command == "profile-actions":
        print('mnemonic("^TestRunner$", ' + target_set(profile_targets(args.events, args.architecture, args.page_size, hybrid=args.hybrid, kvm_only=args.kvm_only, rc_kernel=args.rc_kernel)) + ")")
    elif args.command == "profile-targets":
        print("\n".join(profile_targets(args.events, args.architecture, args.page_size, hybrid=args.hybrid, kvm_only=args.kvm_only, rc_kernel=args.rc_kernel)))
    elif args.command == "select-profile":
        select_profile(args.profile, args.architecture, args.events, args.actions, args.output, syscall_policy=args.syscall_policy, page_size=args.page_size, hybrid=args.hybrid, kvm_only=args.kvm_only, rc_kernel=args.rc_kernel, selected_buckets=args.syscall_bucket, selected_owners=args.owner, mixed_rc_owners=args.mixed_rc_owners, local_test_requirements=args.local_test_requirements)
    elif args.command == "combine-rc-profiles":
        combine_rc_profiles(args.directory, args.owner)
    else:
        expected = set(owner_labels(args.targets, allow_empty=args.profile is not None))
        for profile in args.profile or []:
            expected.update(configured_tests(profile))
        actual = configured_tests(args.events)
        if actual.keys() != expected:
            raise ValueError(f"Combined profile changes explicit owners: {sorted(actual.keys() ^ expected)}")


if __name__ == "__main__":
    main()
