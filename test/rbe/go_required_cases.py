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

"""Author strict Go classname/name manifests using configured generation actions.

Usage: go_required_cases.py input.json output.json --evidence joins.json \
    -- <the same Bazel build options as the tests>
Input maps public frontends or raw go_test labels to explicit Go case names.
"""

import argparse
import json
import math
import posixpath
import subprocess
import sys
import time
from pathlib import Path

# SpawnAction.argv is expanded by Bazel, including parameter-file arguments.
# Only the generating label and package import path leave the formatter.
FORMAT = '''json.encode([
    [str(target.label),
     [a.argv[i + 1] if i + 1 < len(a.argv) else None
      for i in range(len(a.argv)) if a.argv[i] == "-pkgname"] +
     [arg[len("-pkgname="):] for arg in a.argv if arg.startswith("-pkgname=")]]
    for a in getattr(target, "actions", []) if a.mnemonic == "GoTestGenTest"
])'''


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError(f"Expected a nonempty string without controls: {value!r}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--bazel", default="bazel")
    parser.add_argument("--timeout", type=float, default=120)
    own = sys.argv[1:]
    if "--" not in own:
        parser.error("Supply the test's configured Bazel flags after --")
    split = own.index("--")
    args, flags = parser.parse_args(own[:split]), own[split + 1:]
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    cases = json.loads(args.cases.read_text(), object_pairs_hook=unique_object)
    if not isinstance(cases, dict) or not cases:
        raise ValueError("Expected a nonempty object mapping public owners to explicit Go case names")
    selected: dict[str, list[str]] = {}
    for owner, values in cases.items():
        if not owner.startswith("//") or ":" not in owner or any(c.isspace() for c in owner):
            raise ValueError(f"Expected a public main-repository target label: {owner!r}")
        if not isinstance(values, list):
            raise ValueError(f"Expected a case-name array for {owner}")
        names = [text(value) for value in values]
        if not names or len(names) != len(set(names)):
            raise ValueError(f"Expected distinct nonempty Go case names for {owner}")
        selected[owner] = names
    deadline = time.monotonic() + args.timeout
    manifest: dict[str, list[str]] = {}
    evidence: list[dict[str, str]] = []
    for owner, names in selected.items():
        # Exact with_cfg chain: frontend -> transitioned alias -> raw target.
        # attr guards raw rules without exports; no unrelated deps are queried.
        query = (f"let root = {json.dumps(owner)} in "
                 'let first = labels(exports, attr(exports, ".+", $root)) in '
                 'let second = labels(exports, attr(exports, ".+", $first)) in '
                 '$root union $first union $second')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Go case manifest analysis deadline expired")
        result = subprocess.run([args.bazel, "cquery", query, *flags,
                                 "--output=starlark", "--starlark:expr=" + FORMAT],
                                check=True, stdout=subprocess.PIPE, text=True, timeout=remaining)
        actions: list[object] = []
        for line in result.stdout.splitlines():
            row = json.loads(line)
            if not isinstance(row, list):
                raise ValueError(f"Malformed action selection for {owner}: {row!r}")
            actions.extend(row)
        match actions:
            case [[str() as generating_label, [str() as package]]]:
                package = text(package)
            case _:
                raise ValueError(f"Expected one GoTestGenTest action with one -pkgname for {owner}: {actions!r}")
        xml_class = posixpath.basename(posixpath.normpath(package))
        if package.startswith("-") or xml_class in ("", ".", ".."):
            raise ValueError(f"Invalid Go test package import path: {package!r}")
        manifest[owner] = [f"{xml_class}/{name}" for name in names]
        evidence.append({"owner": owner, "generating_label": generating_label, "import_path": package})
    args.evidence.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
    args.output.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
