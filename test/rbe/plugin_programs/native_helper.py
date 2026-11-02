"""Compare host and declared launchers for the real native musl DPDK build."""
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

PRODUCT = "f1db6919e4b42d8491826aaeaeaf18d2d4b78748"
BASE = "a2fb8b5e6455a7336fa20ecdaa00326b30b792f5"
ORIGINAL = "c1210292-5868-4f9a-9ed1-ad1d49308b27"
OLD_ARCHIVE = {"hash": "c31a5dc008783cf4d2f1ad4fcff830adf642a2ba2121c438d4ca6509dd126440", "sizeBytes": "7403456"}
OLD_CONFIG = "8fab57f51b67b2d8ac0aedd2988d3ad61173071fa6625ba23fb60a4b47838597"
PMD = ["net_ring_pmd_info", "net_virtio_pmd_info", "net_virtio_user_pmd_info"]
VFIO = ["pci_vfio_read_config", "pci_vfio_map_resource", "rte_vfio_container_dma_map"]


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def build(r, control=False):
    r.update(source=git("rev-parse", "HEAD"), product=PRODUCT, base=BASE, target=TARGET, started=now(),
             original={"invocation": ORIGINAL, "run": 41406257977, "artifact": 13123707083, "archive": OLD_ARCHIVE},
             arm="host-shell" if control else "declared-shell")
    save(r)
    assert r["source"] == os.environ["GITHUB_SHA"] and git("rev-parse", "HEAD^") == PRODUCT
    assert git("rev-parse", "HEAD^^") == BASE
    assert set(git("diff", "--name-only", BASE, PRODUCT).splitlines()) == {
        "MODULE.bazel", "external/tools/plugin-stack/plugin-stack.BUILD", "test/rbe/plugin_programs/framework.patch"}
    assert set(git("diff", "--name-only", PRODUCT, "HEAD").splitlines()) == {
        ".github/workflows/build.yml", "test/rbe/plugin_programs/native_helper.py"}
    assert git("diff", "--name-only", "HEAD") == ("external/tools/plugin-stack/plugin-stack.BUILD" if control else "")
    r["consumer_sha256"] = sha256(Path("external/tools/plugin-stack/plugin-stack.BUILD").read_bytes()).hexdigest()
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
    bep = TMP / (r["arm"] + ".bep")
    compact = TMP / (r["arm"] + ".compact")
    log = TMP / (r["arm"] + ".log")
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
    if rc and not control:
        return
    owner, = [a for a in r["actions"] if a["targetLabel"] == OWNER and a["mnemonic"] == "CcMakeMakeRule"]
    props = {p["name"]: p["value"] for p in owner["platform"]}
    assert props == {"Arch": "amd64", "OSFamily": "linux", "container-image": NATIVE,
                     "recycle-runner": "false", "workload-isolation-type": "oci"}
    assert owner["runner"] == "remote" and not owner["cacheHit"]
    if control:
        text = log.read_text(errors="replace")
        r["missing_host_shell"] = bool(re.search(r"/bin/bash[^\n]*(?:no such file or directory|not found)", text, re.I))
        r["log_sha256"] = sha256(log.read_bytes()).hexdigest()
        save(r)
        assert rc and owner["args"][0] == "/bin/bash" and (owner["exitCode"] or owner["status"])
        assert r["missing_host_shell"]
        r.update(build_verified=True, status="EXPECTED_FAILURE", phase="verified-control")
        save(r)
        return
    assert owner["args"][0].endswith("plugin_programs/bin/bash")
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
    r.update(phase="verified-outputs", finished=now())


def launch_inputs(r):
    from cas_inputs import blob, cas, input_file
    make, = [a for a in r["actions"] if a["targetLabel"] == OWNER and a["mnemonic"] == "CcMakeMakeRule"]
    proof = r["launch"] = {"verified": False}
    save(r)
    action = cas(make["digest"], "Action")
    command = cas(action["commandDigest"], "Command")
    args = command["arguments"]
    assert args == make["args"] and len(args) == 2
    root = action["inputRootDigest"]
    selected = {"bash": args[0], "wrapper": args[1],
                "script": args[1].removesuffix("wrapper_build_script.sh") + "build_script.sh",
                "busybox": args[0].removesuffix("bash") + "awk"}
    content = {}
    proof["inputs"] = {}
    for name, path in selected.items():
        node = input_file(root, path)
        data = blob(node["digest"], r["invocation"])
        content[name] = data
        proof["inputs"][name] = {"path": path, "digest": node["digest"], "executable": node.get("isExecutable", False)}
        save(r)
        if name in ("bash", "busybox"):
            local = TMP / ("declared-" + name)
            local.write_bytes(data)
            header = subprocess.check_output(["readelf", "-h", local], text=True)
            segments = subprocess.check_output(["readelf", "-l", local], text=True)
            dynamic = subprocess.check_output(["readelf", "-d", local], text=True)
            assert data.startswith(b"\x7fELF") and "Advanced Micro Devices X86-64" in header
            assert "INTERP" not in segments and "(NEEDED)" not in dynamic and node.get("isExecutable")
            proof["inputs"][name].update(machine="x86_64", interpreter=None, needed=[])
    wrapper = content["wrapper"].decode()
    script = content["script"].decode()
    proof["inner_uses_declared_shell"] = ('"$EXT_BUILD_ROOT/' + args[0] + '" "$BUILD_SCRIPT" &> "$BUILD_LOG"') in wrapper
    proof["make_uses_declared_shell"] = "'SHELL:=$(DPDK_BUILD_SHELL)'" in script
    proof["consumer_shell_env"] = 'DPDK_BUILD_SHELL="${BASH}"; export DPDK_BUILD_SHELL' in script
    proof["consumer_utility_path"] = 'PATH="${BASH%/*}' in script
    assert all(proof[k] for k in ("inner_uses_declared_shell", "make_uses_declared_shell", "consumer_shell_env", "consumer_utility_path"))
    proof["verified"] = True
    save(r)


def failure(r, key, exc):
    r[key] = {"type": type(exc).__name__, "line": traceback.extract_tb(exc.__traceback__)[-1].lineno}
    save(r)


if __name__ == "__main__":
    metadata = sys.argv[1:] == ["--outputs"]
    r = json.loads((OUT / "result.json").read_text())
    if metadata:
        # Output recovery and launch-input proof are independent; preserve the
        # actual compilation even if one metadata endpoint is unavailable.
        if r.get("build_verified"):
            for key, operation in (("output_error", collect), ("launch_error", launch_inputs)):
                try:
                    operation(r)
                except Exception as exc:
                    failure(r, key, exc)
        passed = r.get("proof", {}).get("verified") and r.get("launch", {}).get("verified") and r.get("control", {}).get("build_verified")
        r.update(status="PASS" if passed else ("FAIL" if r.get("build_exit") else "INCOMPLETE"), verdict_time=now())
        save(r)
        raise SystemExit(0 if passed else 1)
    path = Path("external/tools/plugin-stack/plugin-stack.BUILD")
    original = path.read_bytes()
    needle = b'    build_shell = "@plugin_programs//:bin/bash",\n'
    assert original.count(needle) == 2
    control = {"status": "INCOMPLETE"}
    try:
        path.write_bytes(original.replace(needle, b""))
        build(control, control=True)
    except Exception as exc:
        failure(control, "collection_error", exc)
    finally:
        path.write_bytes(original)
    r = {"status": "INCOMPLETE", "control": control}
    save(r)
    try:
        build(r)
    except Exception as exc:
        failure(r, "collection_error", exc)
    finally:
        r["source_clean"] = not git("diff", "--name-only", "HEAD")
        save(r)
    raise SystemExit(0 if r.get("build_verified") and r["source_clean"] else 1)
