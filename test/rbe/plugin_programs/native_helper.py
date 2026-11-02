"""Verify the supported GNU archive on the previously observed one-file worker."""
from collections import Counter
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback
from zipfile import ZipFile

from action_log import context
from qualify import COMPILER_IMAGE, CONFIG, HELPER, OUT, OWNER, TARGET, TMP, audit, diagnostics, inspect_outputs, now, save

PRODUCT = "1902df93dcc46803a60b5ede1c5d234fb265c6f0"
BASE = "388a7cacc45e1fff44c845eb99254c1f3b584fba"
OLD_CONFIG = "8fab57f51b67b2d8ac0aedd2988d3ad61173071fa6625ba23fb60a4b47838597"
PMD = ["net_ring_pmd_info", "net_virtio_pmd_info", "net_virtio_user_pmd_info"]
VFIO = ["pci_vfio_read_config", "pci_vfio_map_resource", "rte_vfio_container_dma_map"]


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def selection_roles(rows):
    roles = {}
    for row in rows:
        bash, wrapper = row["argv"]
        assert bash == "external/+_repo_rules5+plugin_programs/bin/bash"
        assert row["exec_platform"] == "//test/rbe/empty_worker:amd64_musl"
        match = re.fullmatch(r"bazel-out/k8-opt(-exec-ST-[a-f0-9]+)?/bin/external/\+_repo_rules\+libpluginstack/"
                             r"(libpluginstack|pmd_config)_foreign_cc/wrapper_build_script[.]sh", wrapper)
        assert match and re.fullmatch("[a-f0-9]{64}", row["aquery_key"])
        name = match[2]
        assert row["target"] == (OWNER if name == "libpluginstack" else CONFIG)
        role = name + ("_exec" if match[1] else "_target")
        assert role not in roles
        roles[role] = row
    assert set(roles) == {"libpluginstack_target", "pmd_config_target", "pmd_config_exec"}
    assert len({row["aquery_key"] for row in rows}) == 3
    return roles


def resume_selection(r):
    path = TMP / "original.zip"
    digest = sha256(path.read_bytes()).hexdigest()
    assert digest == "0438e45c201d388518ccca7ac7eab0d44fde13e10d4aed41d1b48e3d55d61a95"
    with ZipFile(path) as z:
        old = json.loads(z.read("result.json"))
    assert old["source"] == "04ee6cb7818a57de26aec412ea1484965e58ad40" and old["product"] == PRODUCT
    assert not old.get("build_started") and not old.get("actions") and old["status"] == "INCOMPLETE"
    assert old["arm64"]["exit"] == 1 and old["arm64"]["verified"] and all(old["arm64"]["evidence"].values())
    assert old["arm64"]["target_platform"] == "//tools/bazeldefs:linux_arm64" and "--nobuild" in old["arm64"]["args"]
    assert old["root_proof"]["zip_sha256"] == "22b16a181a64e5102f9ebb4d401b84e25b2977dd9c6268c0b3502167f4450f53"
    assert old["root_proof"]["inventory"]["image"] == COMPILER_IMAGE and old["root_proof"]["tmp"] == ["absent"]
    selection = old["selection"]
    assert selection["exit"] == 0 and not selection["verified"]
    roles = selection_roles(selection["actions"])
    r.update(arm64=old["arm64"], root_proof=old["root_proof"],
             selection={"roles": roles, "invocation": selection["invocation"], "verified": True},
             original_selection_zip=digest)
    save(r)


def build(r):
    r.update(source=git("rev-parse", "HEAD"), product=PRODUCT, base=BASE, target=TARGET, started=now(),
             original={"run": 41434303723, "artifact": 13135782307},
             arm="amd64-gnu")
    save(r)
    assert r["source"] == os.environ["GITHUB_SHA"] and git("rev-parse", PRODUCT + "^") == BASE
    assert git("merge-base", PRODUCT, "HEAD") == PRODUCT
    src = "external/tools/plugin-stack/plugin-stack.BUILD"
    assert git("diff", "--name-only", BASE, PRODUCT) == src
    assert set(git("diff", "--name-only", PRODUCT, "HEAD").splitlines()) <= {
        ".github/workflows/build.yml", "test/rbe/empty_worker/BUILD", "test/rbe/empty_worker/probe.bzl",
        "test/rbe/plugin_programs/qualify.py", "test/rbe/plugin_programs/native_helper.py", "test/rbe/plugin_programs/scratch_root.py"}
    original = subprocess.check_output(["git", "show", PRODUCT + ":" + src])
    assert Path(src).read_bytes() == original and not git("diff", "--name-only", "HEAD")
    platform = "//test/rbe/empty_worker:amd64_musl"
    bazel = ["bazel", "--bazelrc=" + str(TMP / "sidecar.bazelrc")]
    flags = ["--config=x86_64", "--config=plugin-tldk", "-c", "opt",
             "--host_platform=" + platform,
             "--extra_execution_platforms=" + platform, "--remote_accept_cached", "--noremote_upload_local_results",
             "--noremote_local_fallback", "--spawn_strategy=remote", "--remote_download_outputs=toplevel",
             "--jobs=400", "--color=no", "--curses=no", "--registry=" + (Path(os.environ["GITHUB_WORKSPACE"]) / "registry").as_uri(),
             "--registry=https://bcr.bazel.build"]
    image = r["image"] = COMPILER_IMAGE
    r["target_platform"] = "//tools/bazeldefs:linux_amd64"
    resume_selection(r)
    query = TARGET + ' + filter("(:(pmd_config|pmdinfogen|make_built)$|^@@?make[+]?//:make$)", deps(' + TARGET + '))'
    query_bep = TMP / (r["arm"] + ".query.bep")
    p = subprocess.run([*bazel, "cquery", *flags, "--build_event_json_file=" + str(query_bep), "--output=label", query], text=True, capture_output=True, timeout=420)
    selected = [s.split()[0] for s in p.stdout.splitlines() if s.startswith(("@", "//"))]
    r["preflight"] = {"exit": p.returncode, "selected": selected, **diagnostics(p.stderr)}
    save(r)
    if query_bep.exists():
        for line in query_bep.read_text().splitlines():
            event = json.loads(line)
            if "started" in event:
                r["preflight"]["invocation"] = r["invocation"] = event["started"]["uuid"]
    if p.returncode:
        r["status"] = "FAIL"
        r["preflight"]["stderr_sha256"] = sha256(p.stderr.encode()).hexdigest()
        save(r)
        r["preflight"]["causal_context"] = context(p.stderr)
    save(r)
    assert p.returncode == 0
    owners = {TARGET: OWNER, "@libpluginstack//:pmd_config": CONFIG,
              "@plugin_dpdk//:pmdinfogen": HELPER, "@make//:make": "@@make+//:make"}
    assert {owners.get(s, s) for s in selected} == {OWNER, CONFIG, HELPER, "@@make+//:make"}
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
    r["source_matches"] = Path(src).read_bytes() == original and not git("diff", "--name-only", "HEAD")
    save(r)
    if rc:
        return
    owner, = [a for a in r["actions"] if a["targetLabel"] == OWNER and a["mnemonic"] == "CcMakeMakeRule"]
    props = {p["name"]: p["value"] for p in owner["platform"]}
    assert props == {"Arch": "amd64", "OSFamily": "linux", "container-image": image,
                     "recycle-runner": "false", "workload-isolation-type": "oci"}
    assert owner["runner"] == "remote" and not owner["cacheHit"]
    roles = r["selection"]["roles"]
    assert owner["args"] == roles["libpluginstack_target"]["argv"]
    assert not owner["exitCode"] and not owner["status"]
    support = [a for a in r["actions"] if a["targetLabel"] in (CONFIG, HELPER)]
    assert all(not a["exitCode"] and not a["status"] for a in support)
    configs = [a for a in support if a["targetLabel"] == CONFIG and a["mnemonic"] == "CcMakeMakeRule"]
    config, = [a for a in configs if a["args"] == roles["pmd_config_exec"]["argv"]]
    assert {p["name"]: p["value"] for p in config["platform"]} == props
    assert any(a["targetLabel"] == HELPER and a["mnemonic"] == "CppLink" for a in support)
    compiles = [a for a in support if a["targetLabel"] == HELPER and a["mnemonic"] == "CppCompile"]
    assert compiles and all(a["target_triples"] == ["x86_64-linux-musl"] for a in compiles)
    assert r["target_completed"] and r["source_matches"]
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
    selected = [archive, root + "/bin/dpdk-pmdinfogen", root + "/rte_config.h", root + "/pmd_rte_config.h", root + "_foreign_cc/Make.log"]
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
    save(r)
    proof["matching_configuration"] = (TMP / "rte_config.h").read_bytes() == (TMP / "pmd_rte_config.h").read_bytes()
    proof["matches_previous_configuration"] = proof["outputs"]["configuration"]["sha256"] == OLD_CONFIG
    assert proof["matching_configuration"]
    log = (TMP / "Make.log").read_text(errors="replace")
    recipes = Counter(re.findall(r"^\s*(HOSTCC|HOSTLD|PMDINFO)\s+(\S+)\s*$", log, re.M))
    proof["pmd_generation"] = sorted(n for rule, n in recipes if rule == "PMDINFO")
    assert proof["pmd_generation"] and proof["outputs"]["archive"]["pmd_info_symbols"] == PMD
    proof["archive_api"] = archive_api(TMP / "libpluginstack.a")
    save(r)
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
    proof["make_uses_declared_shell"] = '"SHELL:=$BASH"' in script and "DPDK_BUILD_SHELL" not in script
    proof["consumer_utility_path"] = 'PATH="${BASH%/*}' in script
    assert all(proof[k] for k in ("inner_uses_declared_shell", "make_uses_declared_shell", "consumer_utility_path"))
    proof["target_gnu"] = bool(re.search(r"-target(?:=|\s+)x86_64-linux-gnu\b", script))
    proof["external_pmd_generator"] = "PMDINFO_GEN=$(PMDINFOGEN)" in script and "pmdinfogen" in script
    assert proof["target_gnu"] and proof["external_pmd_generator"]
    proof["action_workspace_tmpdir"] = 'TMPDIR="${BUILD_TMPDIR}"' in script
    assert proof["action_workspace_tmpdir"]
    proof["command_env"] = {e["name"]: e["value"] for e in command.get("environmentVariables", [])
                            if e["name"] in ("PATH", "TMPDIR") and re.fullmatch(r"[A-Za-z0-9_:./-]*", e["value"])}
    proof["verified"] = True
    save(r)


def failure(r, key, exc):
    r[key] = {"type": type(exc).__name__, "line": traceback.extract_tb(exc.__traceback__)[-1].lineno}
    save(r)


if __name__ == "__main__":
    if sys.argv[1:] == ["--outputs"]:
        r = json.loads((OUT / "result.json").read_text())
        if r.get("build_verified"):
            for key, operation in (("output_error", collect), ("launch_error", launch_inputs)):
                try:
                    operation(r)
                except Exception as exc:
                    failure(r, key, exc)
        passed = all((r.get("source_clean"), r.get("source_matches"), r.get("build_verified"), r.get("selection", {}).get("verified"),
                      r.get("proof", {}).get("verified"), r.get("launch", {}).get("verified"), r.get("arm64", {}).get("verified")))
        r.update(status="PASS" if passed else "FAIL" if r.get("build_exit") else "INCOMPLETE", verdict_time=now())
        save(r)
        raise SystemExit(0 if passed else 1)

    assert not sys.argv[1:]
    r = {"status": "INCOMPLETE"}
    try:
        build(r)
    except Exception as exc:
        failure(r, "collection_error", exc)
    finally:
        r["source_clean"] = not git("diff", "--name-only", "HEAD")
        save(r)
    raise SystemExit(0 if r.get("build_verified") and r["source_clean"] else 1)
