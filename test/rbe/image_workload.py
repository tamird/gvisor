"""Qualify reference-clock interface compatibility and its existing owners."""
import json
import os
from pathlib import Path


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    options = ["--compilation_mode=opt", "--lockfile_mode=error",
               "--remote_download_outputs=all", "--noannounce_rc",
               "--build_event_json_file_path_conversion=false"]
    builds = ["//runsc/boot:boot"]
    analyzers = ["//pkg/sentry/time:time_nogo", "//runsc/config:config_nogo",
                 "//runsc/boot:boot_nogo"]
    failed = False
    for architecture in ("x86_64", "aarch64"):
        selected = [*options, "--config=" + architecture,
                    "--platforms=" + config["image_platforms"][architecture]]
        built = invoke("boot-" + architecture, ["build", *selected, *builds],
                       "build", targets=builds)
        failed |= built["status"] != "PASS"
        checked = invoke("analyzers-" + architecture,
                         ["test", *selected, "--nocache_test_results", *analyzers],
                         "test", targets=analyzers)
        failed |= checked["status"] != "PASS"
        if architecture == "x86_64":
            tests = ["//pkg/sentry/time:time_test", "//runsc/config:config_test"]
            tested = invoke("unit-" + architecture,
                            ["test", *selected, "--nocache_test_results", *tests],
                            "test", targets=tests)
            failed |= tested["status"] != "PASS"
    if failed:
        raise RuntimeError("Reference-clock build, tests or analyzers failed")
