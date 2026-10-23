"""Bun-specific expectations over the shared retained image verifier."""
import argparse
import json
from pathlib import Path
import re

import qualification_report as report
from image_result import (
    ImageBundle,
    project_retained_image,
    retained_test_report,
    retained_test_xml,
    verify_mixed_graph,
    verify_mixed_oci_images,
    verify_retained_runtime,
    verify_uncached_attempt,
)


def verify(prefix: Path, bundle: ImageBundle, receipt: dict, plan: dict, config: dict,
           metadata: Path, script: Path) -> dict:
    graph_only = config.get("graph_only", False)
    preflight = config.get("compiler_preflight", False)
    assert not preflight or graph_only
    assert plan["expected_native_phases"] == (6 if preflight else 5 if graph_only else 9)
    package, name = "//images/benchmarks/bun-spawn:", "bun_spawn"
    if graph_only:
        verify_mixed_graph(bundle, package, name)
        assert receipt["executed_attempt_count"] == 0 and not receipt["build_outputs"]
        return {}
    image = json.loads(bundle.members["bun-contents.json"])
    outputs, audit = verify_mixed_oci_images(
        bundle, receipt, config, image, package, name, metadata, 1,
        Path(str(prefix) + "-mixed-events.jsonl"))
    expected_script = report.file_record(script)
    for row in image["architectures"]:
        assert {name for name, item in row["files"].items() if item["type"] != "5"} == {"spawn.js"}
        installed = row["files"]["spawn.js"]
        assert (installed["mode"], installed["uid"], installed["gid"], installed["sha256"], installed["bytes"]) == (
            0o644, 0, 0, expected_script["sha256"], expected_script["bytes"])
    prepared = json.loads(bundle.members["bun-runtime-preparation.json"])
    runtime = json.loads(bundle.members["bun-runtime-results.json"])
    retained = verify_retained_runtime(
        bundle, config, runtime, [prepared], image["architectures"][0], "bun",
        Path(str(prefix) + "-runtime-supplier-events.jsonl"))
    assert runtime["version"] == "1.4.3"
    attempt = verify_uncached_attempt(bundle, "bun-consumer", "//test/benchmarks/base:bun_spawn_test")
    assert runtime["consumer"]["native_attempts"] == [attempt] and receipt["executed_attempt_count"] == 1
    [benchmark] = attempt["benchmarks"]
    assert attempt["benchmarks"] == runtime["consumer"]["benchmarks"]
    assert re.fullmatch(r"BenchmarkBunSpawn(?:-\d+)?", benchmark["name"])
    assert benchmark["iterations"] == 100 and set(benchmark["metrics"]) == {"ns/op"}
    assert json.loads(bundle.members["bun-declared-benchmarks.json"]) == {
        "owners": {"//test/benchmarks/base:bun_spawn_test": [
            {"name": "BenchmarkBunSpawn", "count": 1, "iterations": 100, "units": ["ns/op"]}]}}
    retained_test_xml(bundle, "bun-consumer", runtime["consumer"]["xml"])
    retained_test_report(bundle, "bun-consumer", "log", runtime["consumer"]["log"])
    return {"outputs": outputs, "audit": audit, "upstream_image": image,
            "runtime": runtime, "runtime_supplier": retained}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prefix", type=Path)
    parser.add_argument("metadata", type=Path)
    parser.add_argument("script", type=Path)
    parser.add_argument("--manual", type=int, default=0)
    args = parser.parse_args()
    references = [Path(__file__), args.script, *[args.metadata / (name + ".json") for name in
                  ("index", "amd64-manifest", "amd64-config", "arm64-manifest", "arm64-config")]]
    return project_retained_image(
        args.prefix, args.manual,
        lambda prefix, bundle, receipt, plan, config: verify(prefix, bundle, receipt, plan, config, args.metadata, args.script),
        references,
        "Pinned official Bun base plus unchanged script. Graph-only grants no build/runtime credit; full run checks both image platforms and one AMD64 functional benchmark. Runtime is retained from the separately authenticated release source, not rebuilt at the image source. No ARM runtime or comparative performance claim.")


if __name__ == "__main__":
    raise SystemExit(main())
