"""Stage the original qualified bytes and run one native external-traffic owner."""

import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import zipfile

from consumer import RUNTIME, devices, test_case
from external_traffic import IMAGE, error
from qualify import OUT, TMP, now, save
from resume_consumer import write_original
from gnu_external_driver import FIXTURE


PARENT = "442ed51f7e10ba007f686cddc12824c47867dabe"
PRODUCT = "675d49b1c7a1b43ac0d2a5cbe4ab5d897ff44afa"
ORIGINAL = "ca7396df-9b3d-415f-9c0c-8899dbaaf9ad"
BUILD_ZIP = "8dc642894c22c3028af5da8d31fe1d0a1676c80a1ccb63a6b96cb0d653e83e76"
ACTION_ZIP = "75954dcbec9e89366873f995c3902f2fadc5271dd0a3df528fd4ca72f506b204"
CONTROL_ZIP = "cd6ecf81a7dd18b802b121fb191220733b68f4a05716e09d8889f1ea01b06e0c"
INSTALL = TMP / "plugin-installed"
PRIVATE = TMP / "plugin-runtime-logs"
TARGET = "//test/rbe/plugin_programs:gnu_external_traffic"


def stage(r):
    build, actions = TMP / "original.zip", TMP / "actions.zip"
    assert hashlib.sha256(build.read_bytes()).hexdigest() == BUILD_ZIP
    assert hashlib.sha256(actions.read_bytes()).hexdigest() == ACTION_ZIP
    with zipfile.ZipFile(build) as z:
        original = json.loads(z.read("verdict.json"))
        sha_lines = {Path(path).name: sha for sha, path in (line.split() for line in z.read("installed.sha256").decode().splitlines())}
    assert original["product"] == PRODUCT and original["plugin_invocation"] == ORIGINAL
    assert set(sha_lines) == {"runsc", "gvisor_sentry_plugin_stack"}
    with zipfile.ZipFile(actions) as z:
        action = json.loads(z.read("original-actions.json"))
        assert json.loads(z.read("diagnosis.json"))["source"] == PRODUCT
    assert action["status"] == "PASS" and not action["metadataErrors"] and not action["missing"][ORIGINAL]
    assert subprocess.check_output(["git", "rev-parse", "HEAD^"], text=True).strip() == PARENT
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == r["source"]
    r["original"] = {"run": 41206190018, "artifact": 13047222365, "sha256": BUILD_ZIP, "invocation": ORIGINAL,
                     "action_artifact": 13048790798, "action_sha256": ACTION_ZIP, "prior_case": original["status"]}
    files = {path: {"sha256": sha, "bytes": size} for path, sha, size, _ in action["invocations"][ORIGINAL]}
    expect = {"checkpointgofer","gvisor-proc-umounter","gvisor-sentry-prewarmer","gvisor_sentry",
              "gvisor_sentry_plugin_stack","runsc-fd-parking","runsc-metric-server"}
    assert len(action["invocations"][ORIGINAL]) == 8 and set(files) == {"runsc"} | {"gvisor-bin/" + name for name in expect}
    assert all(action["outputs"][path] == info for path, info in files.items())
    assert {Path(k).name: v["sha256"] for k, v in files.items() if Path(k).name in sha_lines} == sha_lines
    r["files"] = {}
    for path, file in files.items():
        r["installing"] = path
        save(r)
        got = write_original({"hash": file["sha256"], "sizeBytes": file["bytes"]}, INSTALL / path, ORIGINAL)
        r["files"][path] = got
        assert got["sha256"] == file["sha256"] and got["bytes"] == file["bytes"]
    del r["installing"]
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
    control_zip = TMP / "control.zip"
    assert hashlib.sha256(control_zip.read_bytes()).hexdigest() == CONTROL_ZIP
    with zipfile.ZipFile(control_zip) as z:
        previous = json.loads(z.read("result.json"))
        native = json.loads(z.read("wire.json"))["runc"]
    assert previous["source"] == PARENT and previous["external"]["status"] == "PASS" and native["ok"]
    assert previous["guest_image"] == r["guest_image"] and native["image"] == image_id
    assert set(native["wire"]) == {"tcp_request","tcp_reply","udp_request","udp_reply"} and native["interface"] == "docker0"
    for proto in ("tcp","udp"):
        h, g = native["host"][proto], native["guest"][proto]
        assert h["sent"] == g["received"] == [257,native["wire"][proto + "_request"]["sha256"]]
        assert h["received"] == g["sent"] == [383,native["wire"][proto + "_reply"]["sha256"]]
    version = os.getenv("ImageVersion")
    os_image = os.getenv("ImageOS")
    reuse = version == "20261025.340.1" and os_image == "ubuntu22" and r["kernel"] == previous["kernel"]
    r["native_basis"] = {"run":41436977329,"artifact":13136054697,"sha256":CONTROL_ZIP,"fixture":FIXTURE,
                         "native_test":previous["external"]["invocation"],"current_image_os":os_image,
                         "current_image_version":version,"previous_image_os":"ubuntu22",
                         "previous_image_version":"20261025.340.1","previous_kernel":previous["kernel"],
                         "previous_image":previous["guest_image"],"reused":reuse}
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
              args, TARGET, "TestGNUExternalTraffic", ("--test_timeout=180", "--test_env=REUSE_RUNC=" + ("yes" if reuse else "no")), owners=("external",))
    r["count_source"] = "retained TestGNUExternalTraffic XML"
    if "cases" not in r["external"]:
        for key in ("case_records", "tests_executed", "passed", "failed", "skipped"):
            r[key] = None
    save(r)
    markers = {"plugin_sentry": 'Sidecar "gvisor_sentry_plugin_stack" found: booting sandbox with ' +
               str(INSTALL / "gvisor-bin/gvisor_sentry_plugin_stack"),
               "plugin_init": "Initializing plugin network stack, config:"}
    recorded = r["runtime_markers"] = dict.fromkeys(markers, False)
    r["runtime_errors"] = []
    for path in PRIVATE.iterdir():
        text = subprocess.check_output(["sudo", "tail", "-c", "4000000", path]).decode(errors="replace")
        for line in text.splitlines():
            if (len(line) < 1500 and len(r["runtime_errors"]) < 10 and
                    any(key in line.lower() for key in ("bad system call", "sigsys", "fatal error:", "unable to attach", "signal: killed"))):
                r["runtime_errors"].append(line[-350:])
        for key, needle in markers.items():
            recorded[key] |= needle in text
    save(r)
    wire = json.loads((OUT / "wire.json").read_text())
    assert set(wire) == ({RUNTIME} if reuse else {"runc", RUNTIME})
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
