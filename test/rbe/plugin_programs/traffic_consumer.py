"""Stage the original qualified bytes and run one native external-traffic owner."""

import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import zipfile

from consumer import PRODUCT, RUNSC, RUNTIME, SENTRY, devices, test_case
from external_traffic import IMAGE, error
from qualify import OUT, TMP, now, save
from resume_consumer import ORIGINAL, write_original


PARENT = "7c639344dfffdb686c1952d9e62adbabea8ff2f6"
ZIP = "a9b8c0bf1dffd0cf2bd78855dee0d06ee912f1cd796c57b3a68852ac73c40b08"
INSTALL = TMP / "plugin-installed"
PRIVATE = TMP / "plugin-runtime-logs"
TARGET = "//test/rbe/plugin_programs:external_traffic"


def stage(r):
    archive = TMP / "original.zip"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == ZIP
    with zipfile.ZipFile(archive) as z:
        original = json.loads(z.read("result.json"))
    assert original["product"] == PRODUCT and original["invocation"] == ORIGINAL
    assert original["status"] == "PASS" and original["socket"]["status"] == "PASS"
    rec = original["recovery"]
    assert rec["carrier"] == PARENT and rec["status"] == "PASS" and not rec["product_rebuilds"]
    assert subprocess.check_output(["git", "rev-parse", "HEAD^"], text=True).strip() == PARENT
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == r["source"]
    r["original"] = {"run": 41377771312, "artifact": 13111992630, "sha256": ZIP, "invocation": ORIGINAL,
                     "launch": rec["outputs"][RUNSC]["action"], "sentry": rec["outputs"][SENTRY]["action"]}
    side = rec["sidecars"]
    assert set(side) == {"checkpointgofer", "gvisor-proc-umounter", "gvisor-sentry-prewarmer",
                         "gvisor_sentry", "runsc-fd-parking", "runsc-metric-server"}
    files = {"gvisor-bin/" + k: v for k, v in side.items()}
    for owner, name in ((RUNSC, "runsc"), (SENTRY, "gvisor-bin/gvisor_sentry_plugin_stack")):
        info = rec["outputs"][owner]
        assert info["link_invocation"] == ORIGINAL and not info["producer_cache_hit"]
        files[name] = info["file"]
    r["files"] = {}
    for path, file in files.items():
        r["installing"] = path
        save(r)
        got = write_original({"hash": file["sha256"], "sizeBytes": file["bytes"]}, INSTALL / path)
        r["files"][path] = got
        assert got == file
    del r["installing"]
    assert r["files"]["runsc"]["sha256"] == original["runtime"]["runsc_sha256"]
    assert r["files"]["gvisor-bin/gvisor_sentry_plugin_stack"]["sha256"] == original["runtime"]["sentry_sha256"]
    r["stage"] = "PASS"


def traffic(r):
    assert r["stage"] == "PASS"
    r["kernel"] = platform.release()
    r["devices"] = devices()
    assert r["devices"]["/dev/vhost-net"] == {"opened": True, "device": [10, 238]}
    r["phase"] = "pull pinned guest"
    save(r)
    subprocess.run(["docker", "pull", IMAGE], check=True, stdout=subprocess.DEVNULL)
    arch, image_id = subprocess.check_output(["docker", "image", "inspect", "-f",
                                             "{{.Architecture}} {{.Id}}", IMAGE], text=True).split()
    assert arch == "amd64"
    r["guest_image"] = {"ref": IMAGE, "image_id": image_id, "arch": arch}
    PRIVATE.mkdir()
    subprocess.run(["sudo", INSTALL / "runsc", "install", "--runtime=" + RUNTIME, "--download-sidecars=NEVER", "--",
                    "--network=plugin", "--platform=systrap", "--debug", "--debug-log=" + str(PRIVATE) + "/"],
                   check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["sudo", "systemctl", "reload", "docker"], check=True)
    entry = json.loads(subprocess.check_output(["sudo", "cat", "/etc/docker/daemon.json"]))["runtimes"][RUNTIME]
    assert entry["path"] == str(INSTALL / "runsc")
    assert {"--network=plugin", "--platform=systrap"} <= set(entry["runtimeArgs"])
    r["phase"] = "native test"
    save(r)
    args = ["--config=x86_64", "--color=no", "--curses=no", "--noremote_upload_local_results",
            "--registry=" + (Path(os.environ["GITHUB_WORKSPACE"]) / "registry").as_uri(),
            "--registry=https://bcr.bazel.build", "--test_env=REPORT_DIR=" + str(OUT)]
    test_case(r, "external", ["bazel", "--bazelrc=" + str(TMP / "sidecar.bazelrc")],
              args, TARGET, "TestExternalTraffic", ("--test_timeout=180",), owners=("external",))
    r["count_source"] = "retained TestExternalTraffic XML"
    if "cases" not in r["external"]:
        for key in ("case_records", "tests_executed", "passed", "failed", "skipped"):
            r[key] = None
    save(r)
    markers = {"plugin_sentry": 'Sidecar "gvisor_sentry_plugin_stack" found: booting sandbox with ' +
               str(INSTALL / "gvisor-bin/gvisor_sentry_plugin_stack"),
               "plugin_init": "Initializing plugin network stack, config:"}
    recorded = r["runtime_markers"] = dict.fromkeys(markers, False)
    for path in PRIVATE.iterdir():
        text = subprocess.check_output(["sudo", "tail", "-c", "4000000", path]).decode(errors="replace")
        for key, needle in markers.items():
            recorded[key] |= needle in text
    save(r)
    wire = json.loads((OUT / "wire.json").read_text())
    assert set(wire) == {"runc", RUNTIME}
    for arm in wire.values():
        assert arm["ok"] and arm["image"] == image_id and len(arm["wire"]) == 4
    assert r["external"]["status"] == "PASS" and all(recorded.values())
    r["status"] = "PASS"


def main():
    phase, = sys.argv[1:]
    r = ({"source": os.environ["GITHUB_SHA"], "product": PRODUCT, "product_builds": 0, "status": "INCOMPLETE",
          "phase": phase, "started": now()} if phase == "stage" else json.loads((OUT / "result.json").read_text()))
    try:
        (stage if phase == "stage" else traffic)(r)
        return 0
    except Exception as exc:
        r["error"] = error(exc)
        return 1
    finally:
        r["finished"] = now()
        save(r)


if __name__ == "__main__":
    raise SystemExit(main())
