# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Check the single-fixture, alternating measurements before invoking benchstat."""

import hashlib
import json
from pathlib import Path
import re
import sys
import tarfile


NGINX_TAG = "gvisor.dev/images/benchmarks/nginx:latest"
SOURCE_TAG = "gvisor.dev/images/benchmarks/nginx:9bcb7654c4976396"
ARCHIVE_SHA256 = "7473b446ad5db2be893a066d322606a43eeb01a687c5f8b4e2dee0ef57d86ea9"
CONFIG_SHA256 = "68ea51fa200bcc3d957c6c4910eb6bfdccb2790d1819970012202b520c17f2f6"


def image_identity(archive: Path) -> dict[str, object]:
    """Read the Docker-save image that actually supplies every measurement."""
    digest = hashlib.sha256()
    with archive.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
    with tarfile.open(archive) as t:
        manifest = json.load(t.extractfile("manifest.json"))
        match [entry for entry in manifest if NGINX_TAG in entry["RepoTags"]]:
            case [entry]:
                config_bytes = t.extractfile(entry["Config"]).read()
            case entries:
                raise ValueError(f"expected one nginx manifest, found {entries}")
    config = json.loads(config_bytes)
    return {
        "archive_sha256": digest.hexdigest(),
        "id": "sha256:" + hashlib.sha256(config_bytes).hexdigest(),
        "os": config["os"],
        "arch": config["architecture"],
        "diff_ids": config["rootfs"]["diff_ids"],
        "tags": sorted(entry["RepoTags"]),
    }


def check_image(archive: Path, inspect: Path) -> None:
    source = image_identity(archive)
    match json.loads(inspect.read_text()):
        case [image]:
            loaded = {"id": image["Id"], "os": image["Os"], "arch": image["Architecture"],
                      "diff_ids": image["RootFS"]["Layers"], "tags": sorted(image["RepoTags"])}
        case images:
            raise ValueError(f"expected one loaded nginx image, found {images}")
    expected = {"archive_sha256": ARCHIVE_SHA256, "id": "sha256:" + CONFIG_SHA256,
                "os": "linux", "arch": "amd64", "tags": sorted([NGINX_TAG, SOURCE_TAG])}
    print("CHECKPOINT_IMAGE_JSON " + json.dumps(
        {"expected": expected, "archive": source, "docker": loaded}, separators=(",", ":")), flush=True)
    if any(source[k] != v for k, v in expected.items()) or any(source[k] != v for k, v in loaded.items()):
        raise ValueError("loaded nginx fixture does not match the qualified upstream archive and platform")


def main(output: Path) -> None:
    log = (output / "checkpointfile.log").read_text()
    controls = ("TestCheckpointWriterPayload", "TestCheckpointWriterSaveAndCloseErrors", "TestCheckpointWriterDeferredWriteError")
    for name in controls:
        if len(re.findall(rf"^--- PASS: {name} \(", log, re.MULTILINE)) != 1:
            raise ValueError(f"missing unique passing correctness control {name}")
    pattern = r"CHECKPOINT_PAIR_INPUT sha256=([0-9a-f]{64}) state=(\d+) pages=(\d+) page_metadata=(\d+)"
    match re.findall(pattern, log):
        case [(digest, state, pages, page_metadata)]:
            sizes = dict(state=int(state), pages=int(pages), page_metadata=int(page_metadata))
        case inputs:
            raise ValueError(f"expected exactly one checkpoint fixture, found {inputs}")
    if min(sizes.values()) <= 0:
        raise ValueError(f"missing checkpoint component: {sizes}")

    order: list[tuple[int, str]] = []
    results: dict[str, list[str]] = {"CallerBuffer": [], "Statefile": []}
    rows: list[dict[str, object]] = []
    decodes = 0
    units = ("ns/op", "cpu-ns", "B/op", "allocs/op")
    for line in log.splitlines():
        match line.split():
            case [name, "5", *fields] if name.startswith("BenchmarkCheckpointFile/"):
                values = {unit: float(value) for value, unit in zip(fields[::2], fields[1::2], strict=True)}
            case _:
                continue
        if name.removesuffix("-1") == "BenchmarkCheckpointFile/Decode":
            if values.get("state-file-B") != sizes["state"] or values.get("pages-file-B") != sizes["pages"] or values.get("pages-metadata-B") != sizes["page_metadata"]:
                raise ValueError(f"Decode measured a different checkpoint: {line}")
            decodes += 1
            continue
        if not (parsed := re.fullmatch(r"BenchmarkCheckpointFile/Encode/pair=(\d\d)/(Statefile|CallerBuffer)(?:-1)?", name)):
            raise ValueError(f"unexpected benchmark row: {line}")
        pair, arm = int(parsed[1]), parsed[2]
        if min(values[unit] for unit in units) <= 0:
            raise ValueError(f"missing timing/allocation metrics: {line}")
        if values.get("decoded-state-files") != 1 or values.get("encoded-state-file-B", 0) <= 0 or values.get("metadata-keys", 0) <= 0:
            raise ValueError(f"missing decoded state file or metadata: {line}")
        if not (values["initial-capacity-B"] == values["final-capacity-B"] >= values["encoded-state-file-B"]):
            raise ValueError(f"the memory destination grew during measurement: {line}")
        order.append((pair, arm))
        rows.append({"pair": pair, "arm": arm, "metrics": values})
        comparable = " ".join(f"{values[unit]:.17g} {unit}" for unit in units)
        results[arm].append(f"BenchmarkCheckpointFile/Encode 5 {comparable}")

    expected = [(pair, arm) for pair in range(12) for arm in (("CallerBuffer", "Statefile") if pair % 2 else ("Statefile", "CallerBuffer"))]
    if decodes != 1 or order != expected:
        raise ValueError(f"expected one Decode and AB/BA pairs {expected}; got Decode={decodes}, {order}")
    summary = {"fixture_sha256": digest, "fixture_bytes": sizes, "pairs": 12, "iterations_per_sample": 5, "measurements": rows, "correctness_controls": list(controls), "statefile_buffer_bytes": 262144, "added_caller_buffer_bytes": 4096}
    (output / "pairs.json").write_text(json.dumps(summary, indent=2) + "\n")
    # The qualification reporter keeps the Bazel test log even if undeclared
    # outputs are not downloaded. Preserve the exact input and raw samples there.
    print("CHECKPOINT_PAIRS_JSON " + json.dumps(summary, separators=(",", ":")))
    header = "goos: linux\ngoarch: amd64\npkg: gvisor.dev/gvisor/test/benchmarks/checkpointfile\n"
    for arm, lines in results.items():
        (output / f"{arm.lower()}.bench").write_text(header + "\n".join(lines) + "\n")
    print(f"Measured {len(rows)} samples in 12 pairs on original checkpoint {digest}.")


if __name__ == "__main__":
    match sys.argv[1:]:
        case ["--image", archive, inspect]:
            check_image(Path(archive), Path(inspect))
        case [output]:
            main(Path(output))
        case args:
            raise ValueError(f"unexpected arguments: {args}")
