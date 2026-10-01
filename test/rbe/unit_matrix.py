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

"""Augment canonical unit patterns with graph-declared ARM64 variants."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def query_word(value: str) -> str:
    # Bazel query strings preserve backslashes; JSON escaping changes regexes.
    for quote in ('"', "'"):
        if quote not in value:
            return quote + value + quote
    raise ValueError(f"Bazel query cannot quote a word containing both quote types: {value}")


def target_set(labels: list[str]) -> str:
    return "set(" + " ".join(query_word(label) for label in labels) + ")"


def owner_labels(path: str) -> list[str]:
    labels = Path(path).read_text().splitlines()
    if not labels or any(not label.startswith("//") for label in labels):
        raise ValueError(f"Expected nonempty canonical owner labels: {labels}")
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


def test_requirements(actions_path: str) -> dict[str, dict[str, str]]:
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
        properties: dict[str, str] = {}
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or not isinstance(key := entry.get("key"), str)
                or not isinstance(value := entry.get("value", ""), str)
            ):
                raise ValueError(f"Invalid execution property: {entry}")
            properties[key] = value
        if properties.get("Arch") != "arm64" or properties.get("OSFamily") != "linux":
            raise ValueError(f"Unexpected native test routing for {label}: {properties}")
        if properties.get("workload-isolation-type") not in ("oci", "firecracker"):
            raise ValueError(f"Unknown worker requirement for {label}: {properties}")
        if label in requirements and requirements[label] != properties:
            raise ValueError(f"Conflicting shard requirements for {label}")
        requirements[label] = properties
    return requirements


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
    args = parser.parse_args()
    if args.command == "query":
        print(owner_query(args.patterns))
    elif args.command == "actions":
        print('mnemonic("^TestRunner$", ' + target_set([owner + "_arm64" for owner in owner_labels(args.owners)]) + ")")
    else:
        select_variants(args.patterns, args.owners, args.actions, args.output)


if __name__ == "__main__":
    main()
