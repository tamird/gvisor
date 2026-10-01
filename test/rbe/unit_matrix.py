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
import json
import sys
from dataclasses import dataclass
from pathlib import Path


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


def owner_query(patterns_path: str) -> str:
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
    return (
        'attr(tags, "rbe-has-arm64-variant", ' + selection + ") except "
        + "attr(tags, " + manual + ", " + selection + ")"
    )


def test_requirements(
    actions_path: str,
    architecture: str = "arm64",
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
        if properties.get("Arch") != architecture or properties.get("OSFamily") != "linux":
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


def configured_tests(events_path: str) -> dict[str, ConfiguredTarget]:
    """Read this successful analysis invocation's runnable top-level tests."""
    targets: dict[str, ConfiguredTarget] = {}
    skipped: set[tuple[str, str]] = set()
    succeeded = False
    for line_number, line in enumerate(Path(events_path).read_text().splitlines(), start=1):
        event = json.loads(line)
        if not isinstance(event, dict) or not isinstance(event_id := event.get("id"), dict):
            raise ValueError(f"Invalid build event at {events_path}:{line_number}")
        if "buildFinished" in event_id:
            finished = event.get("finished")
            succeeded = isinstance(finished, dict) and finished.get("overallSuccess") is True
        if isinstance(aborted := event.get("aborted"), dict) and aborted.get("reason") == "SKIPPED":
            if not (
                isinstance(completed := event_id.get("targetCompleted"), dict)
                and isinstance(label := completed.get("label"), str)
                and isinstance(configuration := completed.get("configuration"), dict)
                and isinstance(checksum := configuration.get("id"), str)
            ):
                raise ValueError(f"Invalid skipped target identity: {event}")
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
        if "testSize" not in configured:
            continue
        configurations = []
        for child in children:
            if (
                isinstance(child, dict)
                and isinstance(completed := child.get("targetCompleted"), dict)
                and completed.get("label") == label
                and isinstance(configuration := completed.get("configuration"), dict)
                and isinstance(checksum := configuration.get("id"), str)
            ):
                configurations.append(checksum)
        if len(configurations) != 1 or label in targets:
            raise ValueError(f"Ambiguous top-level test identity: {event}")
        targets[label] = ConfiguredTarget(tags, configurations[0])
    for label, checksum in sorted(skipped):
        if (target := targets.get(label)) is not None and target.configuration == checksum:
            del targets[label]
            print(f"Bazel skipped {label} in configuration {checksum}", file=sys.stderr)
    if not succeeded or not targets:
        raise ValueError(f"Expected successful, nonempty test analysis: {events_path}")
    return targets


def profile_targets(events_path: str, architecture: str) -> list[str]:
    targets = configured_tests(events_path)
    if architecture == "amd64":
        return sorted(targets)
    missing = sorted(label for label, target in targets.items() if "rbe-has-arm64-variant" not in target.tags)
    if missing:
        raise ValueError(f"Syscall owners without ARM64 variants: {missing}")
    return sorted(label + "_arm64" for label in targets)


def select_syscalls(
    profile_path: str,
    architecture: str,
    events_path: str,
    actions_path: str,
    output_path: str,
) -> None:
    original = configured_tests(profile_path)
    expected = set(profile_targets(profile_path, architecture))
    configured = configured_tests(events_path)
    if configured.keys() != expected:
        raise ValueError(f"Configured syscall owners differ from profile: {sorted(configured.keys() ^ expected)}")
    requirements = test_requirements(
        actions_path,
        architecture,
        {label: target.configuration for label, target in configured.items()},
    )
    if requirements.keys() != expected:
        raise ValueError(f"Missing syscall TestRunners: {sorted(expected - requirements.keys())}")
    unavailable: dict[str, str] = {}
    policy_excluded: dict[str, str] = {}
    for label, target in original.items():
        variant = label if architecture == "amd64" else label + "_arm64"
        # The standalone RBE syscall lane also leaves Nogo to its own lane.
        # Keep this policy distinct from unavailable runtime capabilities.
        if "nogo" in target.tags:
            policy_excluded[variant] = "Nogo runs in the dedicated nogo lane."
        elif "runsc_kvm" in target.tags:
            unavailable[variant] = "KVM execution is unavailable."
        elif architecture == "arm64" and requirements[variant]["workload-isolation-type"] == "firecracker":
            unavailable[variant] = "ARM64 Firecracker execution is unavailable."
    selected = sorted(expected - unavailable.keys() - policy_excluded.keys())
    Path(output_path).write_text("".join(label + "\n" for label in selected))
    print(json.dumps({
        "profile_architecture": architecture,
        "canonical_syscall_owners": sorted(original),
        "selected_syscall_owners": selected,
        "unavailable_syscall_owners": unavailable,
        "policy_excluded_syscall_owners": policy_excluded,
    }, indent=2))


def universe(patterns_path: str) -> str:
    patterns = [line.split("#", 1)[0].strip() for line in Path(patterns_path).read_text().splitlines()]
    if any("," in pattern for pattern in patterns):
        raise ValueError(f"Aquery universe cannot represent comma-containing patterns: {patterns_path}")
    return ",".join(pattern for pattern in patterns if pattern)


def select_variants(patterns_path: str, owners_path: str, actions_path: str, output_path: str) -> None:
    owners = owner_labels(owners_path)
    expected = {owner + "_arm64" for owner in owners}
    requirements = test_requirements(actions_path)
    if requirements.keys() != expected:
        raise ValueError(f"Missing configured test owners: {sorted(expected - requirements.keys())}")
    unavailable = sorted(label for label, properties in requirements.items() if properties["workload-isolation-type"] == "firecracker")
    selected = sorted(expected - set(unavailable))
    # Keep all original patterns, exclusions and non-test build targets. Only
    # the additional variants bypass wildcard manual filtering; ordinary unit
    # tag filters remain in force for both architectures.
    Path(output_path).write_text(Path(patterns_path).read_text().rstrip() + "\n" + "\n".join(selected) + "\n")
    print(json.dumps({
        "additional_arm64_variants": selected,
        "unavailable_arm64_firecracker": unavailable,
        "unchanged_selection": patterns_path,
        "limitation": "Non-Go/C++ unit owners retain their original AMD64 execution.",
    }, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    query = commands.add_parser("query")
    query.add_argument("patterns")
    actions = commands.add_parser("actions")
    actions.add_argument("owners")
    select = commands.add_parser("select")
    for name in ("patterns", "owners", "actions", "output"):
        select.add_argument(name)
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
    syscalls = commands.add_parser("select-syscalls")
    syscalls.add_argument("profile")
    syscalls.add_argument("architecture", choices=("amd64", "arm64"))
    for name in ("events", "actions", "output"):
        syscalls.add_argument(name)
    verify = commands.add_parser("verify")
    verify.add_argument("targets")
    verify.add_argument("events")
    verify.add_argument("--profile", action="append", help="Successful analysis of additional suite roots")
    args = parser.parse_args()
    if args.command == "query":
        print(owner_query(args.patterns))
    elif args.command == "actions":
        print('mnemonic("^TestRunner$", ' + target_set([owner + "_arm64" for owner in owner_labels(args.owners)]) + ")")
    elif args.command == "select":
        select_variants(args.patterns, args.owners, args.actions, args.output)
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
        print('mnemonic("^TestRunner$", ' + target_set(profile_targets(args.events, args.architecture)) + ")")
    elif args.command == "profile-targets":
        print("\n".join(profile_targets(args.events, args.architecture)))
    elif args.command == "select-syscalls":
        select_syscalls(args.profile, args.architecture, args.events, args.actions, args.output)
    else:
        expected = set(owner_labels(args.targets, allow_empty=args.profile is not None))
        for profile in args.profile or []:
            expected.update(configured_tests(profile))
        actual = configured_tests(args.events)
        if actual.keys() != expected:
            raise ValueError(f"Combined profile changes explicit owners: {sorted(actual.keys() ^ expected)}")


if __name__ == "__main__":
    main()
