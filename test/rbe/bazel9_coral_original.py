"""Reconcile the original Coral rejection without executing Bazel again."""

import ast
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile


RUN = 41332521937
JOB = 124925429975
ARTIFACT = 13094216725
SOURCE = "54016945752ce190e5a707c41053384855007269"
STOCK = "aa767e4c43d011121271551fe24da501a56f729d"
TESTED = "542eadd05f05f309cc0d2344ac4b07e967a67fd1"
PRODUCT = "375b2c57032fa910447d4b81217fde2171303a9b"
CORAL = "8e885509123395299bed6a5f9529fdc1b9751599"
ZIP_SHA = "dc54ecdaef4cb2eb2d11c5124d545a9bba83816b812ed074df719172940488bf"
LOG_SHA = "5c94016f536a6c18399799195882f73ee50d59d95ed28df406a1ea7072104804"
TEMPLATE_SHA = "a97829de7f0249a0bcaa4bf09c5c760dd3524597a7181462a0dfafc7999800ee"
ERRORS = [
    "ERROR: Traceback (most recent call last):",
    "Error in rule: at index 0 of provides, got element of type NoneType, want Provider",
    "ERROR: error loading package '@@+crosstool_extension+crosstool//': initialization of module 'cc_toolchain_config.bzl' failed",
]


def command(args: list[str], *, cwd: Path | None = None, data: bytes | None = None) -> bytes:
    result = subprocess.run(args, cwd=cwd, input=data, capture_output=True, timeout=90)
    if result.returncode:
        raise RuntimeError(f"{args[0]} read/verification failed with exit {result.returncode}")
    return result.stdout


def git(*args: str) -> bytes:
    return command(["git", *args])


def api(path: str) -> bytes:
    return command(["gh", "api", "--allow-escape-sequences", path])


def digest(data: bytes, expected: str) -> None:
    assert hashlib.sha256(data).hexdigest() == expected, "Original bytes changed"


def recover(report: dict[str, object]) -> None:
    run = json.loads(api(f"repos/tamird/gvisor/actions/runs/{RUN}"))
    job = json.loads(api(f"repos/tamird/gvisor/actions/jobs/{JOB}"))
    assert (run["id"], run["head_sha"], run["run_attempt"], run["conclusion"]) == (RUN, SOURCE, 1, "failure")
    assert (job["run_id"], job["name"], job["conclusion"]) == (RUN, "build (amd64)", "failure")
    report["original"] = {"run": RUN, "job": JOB, "carrier": SOURCE, "conclusion": "failure"}
    archive = api(f"repos/tamird/gvisor/actions/artifacts/{ARTIFACT}/zip")
    digest(archive, ZIP_SHA)
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        assert sorted(z.namelist()) == ["original-rpc.json", "verdict.json"]
        original = json.loads(z.read("verdict.json"))
    assert (original["stock"], original["product"], original["arch"], original["complete"]) == (STOCK, TESTED, "amd64", False)
    phases = original["phases"]
    assert set(phases) == {"stock-coral-control", "dirwatch-new", "dirwatch-deleted", "dirwatch-recreated", "release-and-test", "locate-test"}
    control = phases["stock-coral-control"]
    assert (control["source"], control["invocation"], control["exit"], control["ok"]) == (STOCK, "fb4e1ab6-4e88-4321-9e75-2aaccab4ef74", 7, False)
    assert control["errors"] == ERRORS and control["work"] == [], "Unexpected original failure"
    for name, phase in phases.items():
        if name != "stock-coral-control":
            assert phase["ok"] is True and phase["exit"] == 0 and phase["source"] == TESTED, name
    assert set(original["outputs"]) == {"hostarch_test", "runsc", "containerd-shim-runsc-v1", "gvisor-bin/gvisor_sentry", "gvisor-bin/checkpointgofer", "gvisor-bin/runsc-metric-server", "gvisor-bin/runsc-fd-parking", "gvisor-bin/gvisor-sentry-prewarmer", "gvisor-bin/gvisor-proc-umounter"}
    assert all(v["machine"] == 62 and v["size"] > 0 and re.fullmatch(r"[0-9a-f]{64}", v["sha256"]) for v in original["outputs"].values())
    log = api(f"repos/tamird/gvisor/actions/jobs/{JOB}/logs")
    digest(log, LOG_SHA)
    rows = [line.split("stock-coral-control ", 1)[1] for line in log.decode().splitlines() if "stock-coral-control {" in line]
    row, = rows
    assert ast.literal_eval(row) == control, "Original job and artifact disagree"
    assert "##[error]Process completed with exit code 1." in log.decode()
    qualifier = git("show", SOURCE + ":test/rbe/bazel9/qualify.py").decode()
    calls = [node for node in ast.walk(ast.parse(qualifier)) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "phase" and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "stock-coral-control"]
    call, = calls
    assert ast.literal_eval(call.args[1]) == ["query", "@crosstool//:toolchains"]
    old_pattern = ast.literal_eval(call.args[2])
    assert not re.search(old_pattern, "\n".join(ERRORS), re.I)
    assert 'version = "9.3.1"' in qualifier
    tree = git("rev-parse", TESTED + "^{tree}").decode().strip()
    assert tree == git("rev-parse", PRODUCT + "^{tree}").decode().strip()
    template = command(["gh", "api", f"repos/google-coral/crosstool/contents/cc_toolchain_config.bzl.tpl?ref={CORAL}", "-H", "Accept: application/vnd.github.raw+json"])
    digest(template, TEMPLATE_SHA)
    declaration = git("show", STOCK + ":tools/bazeldefs/extensions/coral_crosstool.bzl").decode()
    assert "crosstool-" + CORAL in declaration and "crosstool-bazel9.patch" not in declaration
    folder = Path(os.environ["RUNNER_TEMP"]) / "coral-source"
    folder.mkdir()
    file = folder / "cc_toolchain_config.bzl.tpl"
    file.write_bytes(template)
    # Apply only this template's real stock hunks. The Windows deletion
    # mentions another provider and must not be mistaken for this rule.
    for patch in ("crosstool-arm-dirs.patch", "crosstool-link-flags.patch", "remove_windows_deps.patch"):
        contents = git("show", STOCK + ":tools/" + patch).decode()
        sections = [s for s in contents.split("diff --git ")[1:] if s.startswith("a/cc_toolchain_config.bzl.tpl b/cc_toolchain_config.bzl.tpl\n")]
        for section in sections:
            command(["patch", "-p1", "--batch", "--fuzz=0"], cwd=folder, data=("diff --git " + section).encode())
    configured = file.read_text()
    assert re.findall(r"^\s*provides = (.+),$", configured, re.M) == ["[CcToolchainConfigInfo]"]
    assert not any("CcToolchainConfigInfo" in line for line in configured.splitlines() if line.startswith("load("))
    fix = git("show", PRODUCT + ":tools/crosstool-bazel9.patch").decode()
    assert '+load("@rules_cc//cc:defs.bzl", "CcToolchainConfigInfo", "cc_common")' in fix
    report.update(status="PASS", original_control=control, original_complete=False,
                  corrected_control="PASS_EXPECTED_PROVIDER_REJECTION", corrected_amd64_receipt="PASS",
                  archive={"id": ARTIFACT, "sha256": ZIP_SHA}, original_job_log_sha256=LOG_SHA,
                  tested_source=TESTED, review_source=PRODUCT, identical_product_tree=tree,
                  coral={"source": CORAL, "template_sha256": TEMPLATE_SHA,
                         "configured_template_sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                         "provider": "CcToolchainConfigInfo", "attribution": "Pinned rule's sole provides entry; original diagnostic reports NoneType"},
                  original_matcher=old_pattern,
                  other_phases={name: {"exit": phase["exit"], "ok": phase["ok"]} for name, phase in phases.items() if name != "stock-coral-control"},
                  outputs=original["outputs"], new_bazel_invocations=0, new_tests=0)


def main() -> None:
    report: dict[str, object] = {"status": "INCOMPLETE", "scope": "Original AMD64 Coral control and receipt reconciliation"}
    destination = Path(os.environ["RUNNER_TEMP"]) / "coral-evidence/result.json"
    try:
        recover(report)
    finally:
        report["verified_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        destination.write_text(json.dumps(report, indent=2) + "\n")
    print(report["status"], report["corrected_control"])


if __name__ == "__main__":
    main()
