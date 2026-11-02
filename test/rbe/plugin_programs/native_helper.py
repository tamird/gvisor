"""Check DPDK's native host tool in the ordinary musl archive action."""
from collections import Counter
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback

from qualify import CONFIG, HELPER, NATIVE, OUT, OWNER, TARGET, TMP, audit, diagnostics, inspect_outputs, now, save

PRODUCT = "a5a644b77a52f32e631f3f87febf8e6f7a6d2554"
BASE = "acf4a712219b1b4917d033c4a108d88d142b3861"
ORIGINAL = "d2c2f184-a6b4-4528-92a0-c2309546d07d"
OLD_ARCHIVE = {"hash": "ca0db58c9cc713b3c53c4ec2b05a7e6101cad3b442a26d890290cd1cd1c7aff4", "sizeBytes": "7409840"}
OLD_CONFIG = "8fab57f51b67b2d8ac0aedd2988d3ad61173071fa6625ba23fb60a4b47838597"
PMD = ["net_ring_pmd_info", "net_virtio_pmd_info", "net_virtio_user_pmd_info"]
VFIO = ["pci_vfio_read_config", "pci_vfio_map_resource", "rte_vfio_container_dma_map"]


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def build(r):
    r.update(source=git("rev-parse", "HEAD"), product=PRODUCT, base=BASE, target=TARGET, started=now(),
             original={"invocation": ORIGINAL, "run": 41376238963, "artifact": 13111068733, "archive": OLD_ARCHIVE})
    save(r)
    assert r["source"] == os.environ["GITHUB_SHA"] and git("rev-parse", "HEAD^") == PRODUCT
    assert git("rev-parse", "HEAD^^") == BASE
    assert git("diff", "--name-only", BASE, PRODUCT) == "external/tools/plugin-stack/plugin-stack.BUILD"
    assert set(git("diff", "--name-only", PRODUCT, "HEAD").splitlines()) == {
        ".github/workflows/build.yml", "test/rbe/plugin_programs/native_helper.py"}
    assert not git("diff", "--name-only", "HEAD")
    assert json.loads((OUT / "image.json").read_text())["manifest"] == NATIVE.split("@")[1]
    platform = "//test/rbe/empty_worker:amd64_musl"
    bazel = ["bazel", "--bazelrc=" + str(TMP / "sidecar.bazelrc")]
    flags = ["--config=x86_64", "--config=plugin-tldk", "-c", "opt",
             "--platforms=@llvm//platforms:linux_x86_64_musl", "--host_platform=" + platform,
             "--extra_execution_platforms=" + platform, "--remote_accept_cached", "--noremote_upload_local_results",
             "--noremote_local_fallback", "--spawn_strategy=remote", "--remote_download_outputs=toplevel",
             "--jobs=400", "--color=no", "--curses=no", "--registry=" + (Path(os.environ["GITHUB_WORKSPACE"]) / "registry").as_uri(),
             "--registry=https://bcr.bazel.build"]
    query = TARGET + ' + filter(":(pmd_config|pmdinfogen)$", deps(' + TARGET + '))'
    p = subprocess.run([*bazel, "cquery", *flags, "--output=label", query], text=True, capture_output=True, timeout=420)
    selected = [s.split()[0] for s in p.stdout.splitlines() if s.startswith(("@", "//"))]
    r["preflight"] = {"exit": p.returncode, "selected": selected, **diagnostics(p.stderr)}
    save(r)
    assert p.returncode == 0 and len(selected) == 1 and selected[0] in (TARGET, OWNER)
    bep = TMP / "helper.bep"
    compact = TMP / "helper.compact"
    log = TMP / "helper.log"
    r.update(phase="build", build_started=now())
    save(r)
    with log.open("w") as dst:
        rc = subprocess.run(["timeout", "--kill-after=20s", "16m", *bazel, "build", *flags,
                             "--execution_log_compact_file=" + str(compact), "--build_event_json_file=" + str(bep),
                             "--build_event_json_file_path_conversion=false", TARGET], stdout=dst, stderr=subprocess.STDOUT).returncode
    r.update(build_exit=rc, status="FAIL" if rc else "INCOMPLETE", build_finished=now())
    save(r)
    r.update(diagnostics(log.read_text(errors="replace")))
    ev = [json.loads(s) for s in bep.read_text().splitlines()] if bep.exists() else []
    for e in ev:
        if "started" in e:
            r["invocation"] = e["started"]["uuid"]
        if e["id"].get("targetCompleted", {}).get("label") in (TARGET, OWNER) and "completed" in e:
            r["target_completed"] = e["completed"].get("success", False)
    save(r)
    r["actions"] = audit(compact) if compact.exists() else []
    r["source_clean"] = not git("diff", "--name-only", "HEAD")
    save(r)
    if rc:
        return
    owner, = [a for a in r["actions"] if a["targetLabel"] == OWNER and a["mnemonic"] == "CcMakeMakeRule"]
    props = {p["name"]: p["value"] for p in owner["platform"]}
    assert props == {"Arch": "amd64", "OSFamily": "linux", "container-image": NATIVE,
                     "recycle-runner": "false", "workload-isolation-type": "oci"}
    assert owner["args"][0].endswith("/bin/bash") and owner["runner"] == "remote" and not owner["cacheHit"]
    assert not owner["exitCode"] and not owner["status"]
    assert not any(a["targetLabel"] in (CONFIG, HELPER) for a in r["actions"])
    assert r["target_completed"] and r["source_clean"]
    r.update(build_verified=True, phase="original-outputs")
    save(r)


def archive_api(path):
    symbols = subprocess.check_output(["nm", "--defined-only", "--extern-only", path], text=True, stderr=subprocess.DEVNULL)
    rows = sorted(re.findall(r"^\S+\s+([A-Z] \S+)$", symbols, re.M))
    assert all("T " + name in rows for name in VFIO)
    members = subprocess.check_output(["ar", "t", path], text=True).splitlines()
    assert members and rows
    metadata = sorted(set(re.findall(rb"PMD_INFO_STRING= \{[^\0\r\n]*\}", path.read_bytes())))
    assert sorted(json.loads(m.split(b"= ", 1)[1])["name"] + "_pmd_info" for m in metadata) == PMD
    return {"members": len(members), "members_sha256": sha256("\n".join(members).encode()).hexdigest(),
            "exported_symbols": len(rows), "symbols_sha256": sha256("\n".join(rows).encode()).hexdigest(),
            "driver_metadata": [sha256(m).hexdigest() for m in metadata], "present_vfio_symbols": VFIO}


def collect(r):
    from cas_inputs import blob, rpc
    if not r.get("build_verified"):
        return
    proof = r["proof"] = {"verified": False, "started": now()}
    save(r)
    make, = [a for a in r["actions"] if a["targetLabel"] == OWNER and a["mnemonic"] == "CcMakeMakeRule"]
    actions = rpc("GetExecution", {"executionLookup": {"invocationId": r["invocation"], "actionDigestHash": make["digest"]["hash"]},
                                   "inlineExecuteResponse": True}).get("execution", [])
    action, = [a for a in actions if a.get("actionDigest", {}).get("hash") == make["digest"]["hash"]
               and str(a["actionDigest"]["sizeBytes"]) == str(make["digest"]["sizeBytes"])]
    assert action["stage"] in (4, "COMPLETED")
    response = action["executeResponse"]
    assert not response.get("status", {}).get("code") and not response.get("cachedResult")
    assert response["result"].get("exitCode", 0) == 0
    files = response["result"]["outputFiles"]
    archive, = [f["path"] for f in files if f["path"].endswith("/libpluginstack/lib/libpluginstack.a")]
    root = archive.rsplit("/lib/", 1)[0]
    selected = [archive, root + "/bin/dpdk-pmdinfogen", root + "/rte_config.h", root + "_foreign_cc/Make.log"]
    outputs = {}
    proof["output_digests"] = {}
    for name in selected:
        f, = [f for f in files if f["path"] == name]
        data = blob(f["digest"], r["invocation"])
        local = TMP / Path(name).name
        local.write_bytes(data)
        outputs[local.name] = {local}
        proof["output_digests"][local.name] = f["digest"]
        save(r)
    proof["outputs"] = inspect_outputs(outputs)
    log = (TMP / "Make.log").read_text(errors="replace")
    # These are DPDK's compact recipe names, from rte.hostapp.mk and
    # rte.compile-pre.mk, inside the one selected remote Make action.
    recipes = Counter(re.findall(r"^\s*(HOSTCC|HOSTLD|PMDINFO)\s+(\S+)\s*$", log, re.M))
    proof["in_build_recipes"] = {"compiled_helper": recipes[("HOSTCC", "pmdinfogen.o")],
                                 "linked_helper": recipes[("HOSTLD", "dpdk-pmdinfogen")],
                                 "metadata_files": sorted(n for (rule, n) in recipes if rule == "PMDINFO")}
    save(r)
    assert all(proof["in_build_recipes"].values())
    assert proof["outputs"]["archive"]["pmd_info_symbols"] == PMD
    header = (TMP / "rte_config.h").read_text()
    assert "#define RTE_EAL_VFIO 1" in header and "#define RTE_LIBRTE_PCI_BUS 1" in header
    assert proof["outputs"]["configuration"]["sha256"] == OLD_CONFIG
    proof["new_api"] = archive_api(TMP / "libpluginstack.a")
    save(r)
    previous = TMP / "previous-plugin.a"
    previous.write_bytes(blob(OLD_ARCHIVE, ORIGINAL))
    proof["old_api"] = archive_api(previous)
    proof["byte_identical_archive"] = proof["outputs"]["archive"]["sha256"] == OLD_ARCHIVE["hash"]
    assert proof["new_api"] == proof["old_api"]
    proof.update(verified=True, finished=now())
    r.update(status="PASS", phase="verified", finished=now())


if __name__ == "__main__":
    metadata = sys.argv[1:] == ["--outputs"]
    r = json.loads((OUT / "result.json").read_text())
    try:
        if metadata:
            collect(r)
        else:
            build(r)
    except Exception as e:
        r["collection_error"] = {"type": type(e).__name__, "line": traceback.extract_tb(e.__traceback__)[-1].lineno}
    finally:
        save(r)
    raise SystemExit(0 if (r.get("proof", {}).get("verified") if metadata else r.get("build_verified")) else 1)
