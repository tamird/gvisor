"""Install the already built source independently of original-action recovery."""

import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import traceback
import zipfile

import full as f
import qualify as q

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import runtime_release as guest

ORIGINAL = {
    "artifact": 13457819044,
    "bytes": 11928,
    "sha256": "9a9cd4bfbe978b0cd5db2c9993c16831b3f84e3c6800c30579ce61107046575b",
    "invocations": {"amd64": "b75a4ccf-69d7-4b42-a786-0f22060d4559", "arm64": "36172062-ef0d-4c98-b7b2-c3a425eefde5"},
}


def original(arch):
    raw = (q.TMP / "original.zip").read_bytes()
    assert len(raw) == ORIGINAL["bytes"] and hashlib.sha256(raw).hexdigest() == ORIGINAL["sha256"]
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert set(archive.namelist()) == {"amd64.json", "arm64.json", "original-failures.json"}
        assert all(i.file_size < 65536 for i in archive.infolist())
        row = json.loads(archive.read(arch + ".json"))
    assert row["source"] == q.PRODUCT and row["native_exit"] == 0
    assert row["requested_invocation"] == row["events"]["invocation"] == ORIGINAL["invocations"][arch]
    assert row["events"]["exit"] == 0 and row["events"]["owners"] == [True] and row["events"]["test_results"] == 0
    assert set(row["runners"]) <= {"remote", "remote cache hit"} and row["worker_pin"] == q.PIN["amd64"]
    assert row["status"] == "INCOMPLETE" and row["collection_error"] == "AssertionError" and row["line"] == 236
    assert "make_exit" not in row and set(row["binaries"]) == f.FILES
    assert all(row[k]["status"] == "PASS" for k in ("bootstrap", "generator_outputs", "stateify", "stub_outputs"))
    return row


def install(arch):
    row = {"status": "INCOMPLETE", "arch": arch, "source": q.PRODUCT, "carrier": os.environ["CARRIER_SHA"],
           "run": int(os.environ["QUALIFICATION_RUN_ID"]), "exec_arch": "amd64", "worker_pin": q.PIN["amd64"],
           "scope": "Make build/cquery/install of the original successful source; cache reuse allowed; no TestRunner", "started": q.now()}
    q.save(arch, row)
    try:
        prior = original(arch)
        row["original"] = {**ORIGINAL, "build_invocation": prior["events"]["invocation"]}
        config = ["--config=" + {"amd64": "x86_64", "arm64": "aarch64"}[arch], "-c", "opt",
                  "--extra_execution_platforms=//test/rbe/release_prewarmer:amd64", "--host_platform=//test/rbe/release_prewarmer:amd64",
                  "--extra_toolchains=//test/rbe/go_bootstrap:linux_amd64_ash",
                  "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain", "--keep_going",
                  "--remote_accept_cached", "--remote_download_outputs=toplevel", "--jobs=32",
                  "--lockfile_mode=off", "--color=no", "--curses=no", "--build_event_publish_all_actions"]
        row["flags"] = config
        installed = q.TMP / (arch + "-installation")
        installed.mkdir()
        log = q.TMP / (arch + "-make.log")
        # The actual Make invocation precedes every new provenance assertion.
        with log.open("w") as output:
            call = subprocess.run(["timeout", "--signal=INT", "--kill-after=30", "1800", "make", "copy", "DOCKER_BUILD=false",
                "BAZEL=" + sys.executable + " " + str(Path(__file__).resolve()) + " make-bazel " + arch,
                "BAZEL_OPTIONS=" + " ".join(config), "TARGETS=//:release", "DESTINATION=" + str(installed)],
                stdout=output, stderr=subprocess.STDOUT)
        row.update(make_exit=call.returncode, returned=q.now(), diagnostics=q.diagnostics(log))
        q.save(arch, row)
        row["make_commands"] = []
        for command in ("build", "cquery"):
            base = q.TMP / ("make-" + arch + "-" + command)
            if not Path(str(base) + ".json").is_file():
                continue
            item = json.loads(Path(str(base) + ".json").read_text())
            row["make_commands"].append(item)
            bep = Path(str(base) + ".bep")
            item["events"] = q.events(bep)
            if command == "build":
                row["native_exit"] = item["exit"]
                row["events"] = item["events"]
                item["events"]["workspace_status"] = {}
                for line in bep.open():
                    event = json.loads(line)
                    for entry in event.get("workspaceStatus", {}).get("item", []):
                        if entry["key"] == "STABLE_VERSION":
                            item["events"]["workspace_status"]["STABLE_VERSION"] = entry["value"]
                compact = Path(str(base) + ".compact")
                if compact.exists():
                    item["runners"], item["actions"] = f.actions(compact, "amd64")
                configs, _, roots = f.configured_events(bep)
                item["root_configurations"] = [{"id": c, "cpu": configs[c]} for c in roots]
            q.save(arch, row)
        if call.returncode:
            row["status"] = "FAIL" if call.returncode not in (124, 137) else "INCOMPLETE"
            return row
        build, query = row["make_commands"]
        for item in (build, query):
            assert item["events"]["invocation"] == item["invocation"] and item["events"]["exit"] == item["exit"] == 0
            assert item["events"]["test_results"] == 0
        assert build["events"]["owners"] == [True] and build["events"]["bazel"] == "9.3.1"
        assert build["events"]["workspace_status"]["STABLE_VERSION"] == q.PRODUCT[:12]
        target, = build["root_configurations"]
        assert target["cpu"] == {"amd64": "k8", "arm64": "aarch64"}[arch]
        assert set(build["runners"]) <= {"remote", "remote cache hit"}
        assert all(a["worker_matches"] and a["exit"] == 0 and not a["status"] for a in build["actions"])
        row["installed_files"] = sorted(str(p.relative_to(installed)) for p in installed.rglob("*") if p.is_file())
        assert set(row["installed_files"]) == f.FILES
        row["files"], row["original_byte_matches"] = {}, {}
        for name in row["installed_files"]:
            path = installed / name
            mode = path.lstat().st_mode
            assert stat.S_ISREG(mode) and mode & 0o111 == 0o111
            row["files"][name] = guest.check_file(path, {"amd64": 62, "arm64": 183}[arch])
            row["original_byte_matches"][name] = q.elf(path) == prior["binaries"][name]
            q.save(arch, row)
        assert all(row["original_byte_matches"].values())
        row["status"] = "PASS"
    except (AssertionError, KeyError, ValueError, OSError, subprocess.SubprocessError) as error:
        row.update(collection_error=type(error).__name__, line=traceback.extract_tb(error.__traceback__)[-1].lineno)
    finally:
        row["verified_at"] = q.now()
        q.save(arch, row)
    return row


def main():
    assert not any(os.environ.get(k) for k in ("ACCESS", "BUILDBUDDY_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"))
    mode, *args = sys.argv[1:]
    if mode == "make-bazel":
        return f.make_bazel(args[0], args[1], args[2:])
    assert mode in ("amd64", "arm64") and not args
    assert q.git("rev-parse", "HEAD") == q.PRODUCT and not q.git("diff", "--name-only", "HEAD")
    return int(install(mode)["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
