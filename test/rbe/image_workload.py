"""Build the formerly failing vDSO with the declared compiler and linker."""
import json
import os
from pathlib import Path

import qualification_report as report


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    target = "//vdso:vdso"
    if config["targets"] != [target]:
        raise ValueError("Unexpected freestanding validation target")
    options = [*config["consumer_options"], "--compilation_mode=opt", "--config=x86_64",
               "--lockfile_mode=error", "--remote_download_outputs=all", "--noannounce_rc",
               "--build_event_json_file_path_conversion=false", "--noremote_accept_cached"]
    execution = private / "vdso-execution.json"
    result = {"source": config["source"], "status": "INCOMPLETE", "target": target}
    try:
        try:
            built = invoke("vdso-build", ["build", *options,
                           "--execution_log_json_file=" + str(execution), "--noexecution_log_sort", target],
                           "build", targets=[target])
        finally:
            record_execution(execution, "x86_64", "vdso")
        if built["status"] != "PASS" or built["native_exit"] != 0 or len(built["expected"]) != 1:
            raise RuntimeError("The owning vDSO build did not pass")
        audit = json.loads((evidence / "x86_64-vdso-image-execution.json").read_text())
        actions = [row for row in audit["image_actions"] if row["targetLabel"] == target]
        if (audit["status"] != "PASS" or len(actions) != 1
                or actions[0]["mnemonic"] != "Genrule" or actions[0]["runner"] != "remote"
                or actions[0]["cacheHit"] or actions[0]["exitCode"] != 0):
            raise ValueError("Expected one successful uncached remote vDSO genrule")
        owner = built["expected"][0]
        outputs = report.advertised_build_outputs(evidence / "invocation-vdso-build/events.jsonl",
            {(report.Owner(owner["label"], owner["configuration"]), "default")})
        if len(outputs) != 1 or not outputs[0]["name"].endswith("/vdso.so"):
            raise ValueError("Expected the sole configured vDSO output")
        output = outputs[0]
        match output["uri"].split("/bazel-out/", 1):
            case [_, relative]:
                output_path = "bazel-out/" + relative
            case _:
                raise ValueError("Expected a configured vDSO output path")
        producers = [item for item in actions[0]["actualOutputs"]
                     if item["path"] == output_path
                     and item.get("digest", {}).get("hash") == output["sha256"]
                     and int(item.get("digest", {}).get("sizeBytes", -1)) == output["bytes"]]
        if len(producers) != 1 or output["name"] != output_path:
            raise ValueError("Native vDSO output differs from its configured default BEP output")
        result.update(status="PASS", owner=owner, outputs=outputs, action=actions[0],
                      scope="One AMD64 vDSO build only; no Bun/netip runtime or other architecture credit")
    finally:
        report.write_json(evidence / "vdso-build-check.json", result)
