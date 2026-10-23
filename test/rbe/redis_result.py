"""Redis-specific expectations over the shared retained image verifier."""
import argparse
import json
from pathlib import Path
import re

from image_result import (
    ImageBundle,
    project_retained_image,
    retained_test_xml,
    verify_mixed_graph,
    verify_mixed_oci_images,
    verify_retained_runtime,
    verify_uncached_attempt,
)


def verify(prefix: Path, bundle: ImageBundle, receipt: dict, plan: dict, config: dict, metadata: Path) -> dict:
    graph_only = config.get("graph_only", False)
    assert plan["expected_native_phases"] == (5 if graph_only else 13)
    if graph_only:
        verify_mixed_graph(bundle, "//images/basic:", "redis")
        assert receipt["executed_attempt_count"] == 0 and not receipt["build_outputs"]
        return {}
    image = json.loads(bundle.members["redis-contents.json"])
    outputs, audit = verify_mixed_oci_images(
        bundle, receipt, config, image, "//images/basic:", "redis", metadata, 0,
        Path(str(prefix) + "-mixed-events.jsonl"))
    assert all(not row["files"] for row in image["architectures"])
    runtime = json.loads(bundle.members["redis-runtime-results.json"])
    assert [row["mode"] for row in runtime["modes"]] == ["netstack", "hostnet"]
    prepared = [json.loads(bundle.members["redis-" + mode + "/runtime-preparation.json"])
                for mode in ("netstack", "hostnet")]
    retained = verify_retained_runtime(
        bundle, config, runtime, prepared, image["architectures"][0], "redis",
        Path(str(prefix) + "-runtime-supplier-events.jsonl"))
    for row, setup, network in zip(runtime["modes"], prepared, ("sandbox", "host"), strict=True):
        phase = "redis-" + row["mode"] + "-consumer"
        installed = setup["config_after"]["runtimes"][setup["runtime"]]
        assert setup["server_info"]["Runtimes"][setup["runtime"]] == installed
        [runsc] = [item for item in setup["runtime_files"] if item["path"].endswith("/redis-runtime/runsc")]
        assert installed["path"] == runsc["path"]
        assert [arg for arg in installed["runtimeArgs"] if arg.startswith("--platform=")] == ["--platform=systrap"]
        assert [arg for arg in installed["runtimeArgs"] if arg.startswith("--network=")] == ["--network=" + network]
        arguments = bundle.members["invocation-" + phase + "/arguments.txt"].decode().splitlines()
        assert [arg for arg in arguments if arg.startswith("--test_arg=-runtime=")] == ["--test_arg=-runtime=" + setup["runtime"]]
        attempt = verify_uncached_attempt(bundle, phase, "//test/root:portforward_test")
        assert row["status"] == "PASS" and row["native_attempts"] == [attempt]
        assert row["names"] == ["TestPortForwardLocalMode"]
        assert row["counts"] == {"failed": 0, "passed": 1, "skipped": 0, "failures": [], "skips": []}
        xml = retained_test_xml(bundle, phase, row["xml"])
        assert [node.get("name") for node in xml.iter("testcase")] == row["names"]
    versions = runtime["modes"][0]["versions"]
    assert set(versions) == {"redis-server", "redis-cli"}
    assert re.search(r"^Redis server v=7\.0\.12(?: |$)", versions["redis-server"])
    assert versions["redis-cli"] == "redis-cli 7.0.12"
    nogo = bundle.phases["invocation-redis-nogo/verdict.json"]
    [attempt] = nogo["executed"]
    assert attempt["owner"]["label"] == "//test/root:portforward_test_nogo"
    assert not attempt["cached_locally"] and not attempt["execution_info"].get("cachedRemotely", False)
    assert nogo["case_counts"] == {"failed": 0, "passed": 1, "skipped": 0}
    [log] = [body for name, body in bundle.members.items()
             if name.startswith("invocation-redis-nogo/") and name.endswith("/test.log")]
    assert b"arm64...PASS" in log and b"amd64...PASS" in log
    assert receipt["executed_attempt_count"] == 3
    return {"outputs": outputs, "audit": audit, "upstream_image": image,
            "runtime": runtime, "runtime_supplier": retained}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prefix", type=Path)
    parser.add_argument("metadata", type=Path)
    parser.add_argument("--manual", type=int, default=0)
    args = parser.parse_args()
    references = [Path(__file__), *[args.metadata / (name + ".json") for name in
                  ("index", "amd64-manifest", "amd64-config", "arm64-manifest", "arm64-config")]]
    return project_retained_image(
        args.prefix, args.manual,
        lambda prefix, bundle, receipt, plan, config: verify(prefix, bundle, receipt, plan, config, args.metadata),
        references,
        "Pinned official Redis 7.0.12 with no added layer. Both declared image platforms; current local port-forward consumer under netstack and host networking on AMD64. Seven-file runtime is retained from its separately authenticated source. No ARM runtime, source-built Redis or comparative performance claim.")


if __name__ == "__main__":
    raise SystemExit(main())
