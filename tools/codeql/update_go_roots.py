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

"""Generate production analysis roots from the maintained owning aliases.

Input: bazel query 'attr("tags", "codeql-go-owner:", //...)' --output=xml
Run through //tools/codeql:update_go_roots; --check verifies checked-in drift.
"""

import argparse
import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET


def main_label(label: str) -> str:
    label = label.removeprefix("@@") if label.startswith("@@//") else label
    if not label.startswith("//"):
        raise ValueError(f"Expected a main-repository root: {label}")
    return label


def roots_from_query(query: ET.Element) -> tuple[dict[str, str], dict[str, str], dict[str, list[str]]]:
    roots: dict[str, str] = {}
    excluded: dict[str, str] = {}
    modes: dict[str, list[str]] = {}
    for element in query.findall("rule"):
        label = main_label(element.attrib["name"])
        tags = [item.attrib["value"] for attr in element if attr.attrib.get("name") == "tags" for item in attr]
        match [main_label(tag.removeprefix("codeql-go-owner:")) for tag in tags if tag.startswith("codeql-go-owner:")]:
            case [owner]:
                pass
            case observed:
                raise ValueError(f"Expected one explicit logical owner for {label}: {observed}")
        if (label != owner + "_codeql" or label in roots or label in excluded
                or owner in roots.values() or owner in excluded.values() or "manual" not in tags):
            raise ValueError(f"Duplicate or invalid declared analysis alias: {label}")
        disposition = [tag.removeprefix("codeql-go-excluded:") for tag in tags if tag.startswith("codeql-go-excluded:")]
        if disposition:
            if (element.attrib["class"] != "filegroup" or len(disposition) != len(set(disposition))
                    or set(disposition) - {"race", "coverage"}
                    or not any(attr.attrib.get("name") == "srcs" and len(attr) for attr in element)):
                raise ValueError(f"Invalid configured source disposition: {label}")
            excluded[label], modes[owner] = owner, sorted(disposition)
        else:
            match [main_label(attr.attrib["value"]) for attr in element if attr.attrib.get("name") == "actual"]:
                case [actual] if actual == owner and element.attrib["class"] == "alias":
                    pass
                case observed:
                    raise ValueError(f"Analysis alias changed its declared owner: {label} {observed}")
            roots[label] = owner
    if not roots:
        raise ValueError("No production Go roots were declared")
    return roots, excluded, modes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    output = args.output or Path(os.environ["BUILD_WORKSPACE_DIRECTORY"]) / "tools/codeql/go_roots.bzl"
    roots, excluded, modes = roots_from_query(ET.parse(args.input).getroot())
    contents = '"""Generated production Go roots; update with //tools/codeql:update_go_roots."""\n\nGO_ANALYSIS_ROOTS = {\n'
    contents += "".join(f'    "{label}": "{owner}",\n' for label, owner in sorted(roots.items()))
    contents += "}\n"
    contents += "\nGO_ANALYSIS_EXCLUDED_ROOTS = {\n"
    contents += "".join(f'    "{label}": "{owner}",\n' for label, owner in sorted(excluded.items()))
    contents += "}\n\nGO_ANALYSIS_EXCLUDED_MODES = {\n"
    contents += "".join(f'    "{owner}": {json.dumps(reasons)},\n'
                        for owner, reasons in sorted(modes.items()))
    contents += "}\n"
    if args.check:
        if output.read_text() != contents:
            raise ValueError("Production Go root manifest is stale; regenerate from the query output")
    else:
        output.write_text(contents)
    print(f"Verified {len(roots) + len(excluded)} declared Go owners: {len(roots)} candidates, {len(excluded)} configured dispositions")


if __name__ == "__main__":
    main()
