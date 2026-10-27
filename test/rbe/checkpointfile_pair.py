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

import json
from pathlib import Path
import re
import sys


def main(output: Path) -> None:
    log = (output / "checkpointfile.log").read_text()
    pattern = r"CHECKPOINT_PAIR_INPUT sha256=([0-9a-f]{64}) state=(\d+) pages=(\d+) page_metadata=(\d+)"
    match re.findall(pattern, log):
        case [(digest, state, pages, page_metadata)]:
            sizes = dict(state=int(state), pages=int(pages), page_metadata=int(page_metadata))
        case inputs:
            raise ValueError(f"expected exactly one checkpoint fixture, found {inputs}")
    if min(sizes.values()) <= 0:
        raise ValueError(f"missing checkpoint component: {sizes}")

    order: list[tuple[int, str]] = []
    results: dict[str, list[str]] = {"Codec": [], "Statefile": []}
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
        if not (parsed := re.fullmatch(r"BenchmarkCheckpointFile/Encode/pair=(\d\d)/(Codec|Statefile)(?:-1)?", name)):
            raise ValueError(f"unexpected benchmark row: {line}")
        pair, arm = int(parsed[1]), parsed[2]
        if min(values[unit] for unit in units) <= 0:
            raise ValueError(f"missing timing/allocation metrics: {line}")
        if arm == "Statefile":
            if values.get("decoded-state-files") != 1 or values.get("encoded-state-file-B", 0) <= 0 or values.get("metadata-keys", 0) <= 0:
                raise ValueError(f"missing decoded state file or metadata: {line}")
            if not (values["initial-capacity-B"] == values["final-capacity-B"] >= values["encoded-state-file-B"]):
                raise ValueError(f"the memory destination grew during measurement: {line}")
        elif values.get("encoded-kernel-B", 0) <= 0:
            raise ValueError(f"missing original kernel encoding: {line}")
        order.append((pair, arm))
        rows.append({"pair": pair, "arm": arm, "metrics": values})
        comparable = " ".join(f"{values[unit]:.17g} {unit}" for unit in units)
        results[arm].append(f"BenchmarkCheckpointFile/Encode 5 {comparable}")

    expected = [(pair, arm) for pair in range(12) for arm in (("Statefile", "Codec") if pair % 2 else ("Codec", "Statefile"))]
    if decodes != 1 or order != expected:
        raise ValueError(f"expected one Decode and AB/BA pairs {expected}; got Decode={decodes}, {order}")
    summary = {"fixture_sha256": digest, "fixture_bytes": sizes, "pairs": 12, "iterations_per_sample": 5, "measurements": rows}
    (output / "pairs.json").write_text(json.dumps(summary, indent=2) + "\n")
    # The qualification reporter keeps the Bazel test log even if undeclared
    # outputs are not downloaded. Preserve the exact input and raw samples there.
    print("CHECKPOINT_PAIRS_JSON " + json.dumps(summary, separators=(",", ":")))
    header = "goos: linux\ngoarch: amd64\npkg: gvisor.dev/gvisor/test/benchmarks/checkpointfile\n"
    for arm, lines in results.items():
        (output / f"{arm.lower()}.bench").write_text(header + "\n".join(lines) + "\n")
    print(f"Measured {len(rows)} samples in 12 pairs on original checkpoint {digest}.")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
