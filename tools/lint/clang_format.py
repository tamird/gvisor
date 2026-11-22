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

"""Validate declared formatter configurations and apply canonical patches."""

import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import unquote, urlsplit


CONFIG_NAMES = (".clang-format", "_clang-format", ".clang-format-ignore")


def manifests(events: Path) -> set[Path]:
    """Read formatter manifests from native Bazel output-group events."""
    sets = {}
    roots = []
    with events.open() as stream:
        for line in stream:
            match json.loads(line):
                case {"id": {"namedSet": {"id": str(key)}}, "namedSetOfFiles": dict(files)}:
                    sets[key] = files
                case {"completed": {"outputGroup": list(groups)}}:
                    for group in groups:
                        match group:
                            case {"name": "clang_format_inputs", "fileSets": list(references)}:
                                roots.extend(references)
    result = set()
    visited = set()
    while roots:
        match roots.pop():
            case {"id": str(key)}:
                if key in visited:
                    continue
                visited.add(key)
                files = sets[key]
                roots.extend(files.get("fileSets", []))
                for file in files.get("files", []):
                    match file:
                        case {"name": str(name), "uri": str(uri)} if name.endswith(".clang_format_inputs.json"):
                            location = urlsplit(uri)
                            if location.scheme != "file" or location.netloc:
                                raise ValueError("Formatter manifest is not materialized locally: " + uri)
                            result.add(Path(unquote(location.path)))
            case reference:
                raise ValueError("Invalid Bazel file-set reference: " + repr(reference))
    if not result:
        raise ValueError("No declared formatter inputs in " + str(events))
    return result


def regular(path: Path, root: Path) -> None:
    """Reject missing files and symlink aliases before touching sources."""
    if path.is_absolute() or ".." in path.parts or not path.is_file() or path.resolve() != root / path:
        raise ValueError("Formatter requires an existing file without symlink aliases: " + str(path))


def main() -> None:
    fix, execroot, *events = sys.argv[1:]
    root = Path.cwd().resolve()
    patches: dict[str, Path] = {}
    for manifest in sorted({path for event in events for path in manifests(Path(event))}):
        match json.loads(manifest.read_text()):
            case {"sources": dict(sources), "configs": dict(configs)}:
                pass
            case invalid:
                raise ValueError("Invalid formatter input manifest: " + repr(invalid))
        for name, snapshot in configs.items():
            if not isinstance(name, str) or not isinstance(snapshot, str):
                raise ValueError("Invalid formatter configuration entry")
            config = Path(name)
            regular(config, root)
            if config.read_bytes() != (Path(execroot) / snapshot).read_bytes():
                raise ValueError("Formatter configuration changed during the build: " + name)
        for name, patch in sources.items():
            if not isinstance(name, str) or (patch is not None and not isinstance(patch, str)):
                raise ValueError("Invalid formatter source entry")
            source = Path(name)
            regular(source, root)
            for parent in source.parents:
                for basename in CONFIG_NAMES:
                    config = parent / basename
                    if config.exists() or config.is_symlink():
                        regular(config, root)
                        if str(config) not in configs:
                            raise ValueError("Undeclared formatter configuration: " + str(config) + "; declare it in //:clang_format_config")
            if patch is not None:
                candidate = Path(execroot) / patch
                if previous := patches.get(name):
                    if previous.read_bytes() != candidate.read_bytes():
                        raise ValueError("Conflicting formatter patches for " + name)
                else:
                    patches[name] = candidate
    selected = [str(patch) for patch in patches.values() if patch.stat().st_size]
    if fix == "1" and selected:
        subprocess.run(["git", "apply", "--check", "--", *selected], check=True)
        subprocess.run(["git", "apply", "--", *selected], check=True)


if __name__ == "__main__":
    main()
