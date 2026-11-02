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

from action_log import context
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
    assert r["source"] == os.environ["GITHUB_SHA"] and git("rev-parse", "HEAD~4") == PRODUCT
    assert git("rev-parse", "HEAD~5") == BASE
    assert set(git("diff", "--name-only", BASE, PRODUCT).splitlines()) == {
        "MODULE.bazel", "external/tools/plugin-stack/plugin-stack.BUILD", "test/rbe/plugin_programs/framework.patch"}
    assert set(git("diff", "--name-only", PRODUCT, "HEAD").splitlines()) == {
        ".github/workflows/build.yml", "test/rbe/plugin_programs/native_helper.py",
        "test/rbe/plugin_programs/preflight_recovery.py", "test/rbe/plugin_programs/action_log.py",
        "test/rbe/empty_worker/repository.bzl", "MODULE.bazel"}
    assert git("diff", "--name-only", "HEAD") == ("external/tools/plugin-stack/plugin-stack.BUILD" if control else "")
    module = Path("MODULE.bazel").read_text()
    prior_module = git("show", PRODUCT + ":MODULE.bazel")
    expected_module = prior_module.replace(
        'single_version_override(module_name = "make", version = "4.4.1.bcr.4")',
        'bazel_dep(name = "make", version = "4.4.1.bcr.4")',
    ).replace('    mode = "source",\n    version = "4.4.1",',
              '    mode = "custom",\n    target = "@make//:make",', 1)
    assert module.strip() == expected_module
    r["make_registration"] = {"mode": "custom", "target": "@make//:make", "version": "4.4.1.bcr.4",
                              "module_sha256": sha256(module.encode()).hexdigest()}
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
    assert p.returncode == 0 and len(selected) == 2
    assert len([label for label in selected if label in (TARGET, OWNER)]) == 1
    assert len([label for label in selected if label in ("@make//:make", "@@make+//:make")]) == 1
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


def recover_control(r):
    import base64
    from zipfile import ZipFile
    from cas_inputs import blob, cas, rpc

    source = "16d96cd23039c82aed1f2b38a5a198c43ef76c5e"
    assert git("rev-parse", "HEAD^") == source and git("rev-parse", "HEAD") == os.environ["GITHUB_SHA"]
    assert set(git("diff", "--name-only", source, "HEAD").splitlines()) == {
        ".github/workflows/build.yml", "test/rbe/plugin_programs/native_helper.py"}
    archive = TMP / "original.zip"
    digest = sha256(archive.read_bytes()).hexdigest()
    assert digest == "b970f69f1d5f6011f8a73180955e7fb56c7e635b79fcf70a1f63907371d0dbd6"
    with ZipFile(archive) as z:
        assert set(z.namelist()) == {"result.json", "image.json"}
        original = json.loads(z.read("result.json"))
    r.update(original=original, archive_sha256=digest, original_run=41421129869, artifact=13129732760)
    save(r)
    control = original["control"]
    assert original["status"] == "INCOMPLETE" and original["source"] == control["source"] == source
    assert original["build_exit"] == 0 and original["build_verified"] and original["source_clean"]
    assert original["proof"]["verified"] and original["proof"]["byte_identical_archive"] and original["launch"]["verified"]
    assert control["build_exit"] == 1 and not control["missing_host_shell"] and control["preflight"]["exit"] == 0
    owner, = control["actions"]
    positive, = original["actions"]
    assert owner["targetLabel"] == positive["targetLabel"] == OWNER and owner["mnemonic"] == "CcMakeMakeRule"
    assert owner["platform"] == positive["platform"] and owner["runner"] == "remote" and not owner["cacheHit"]
    assert owner["digest"] == {"hash": "5ed5d7c6db58ca0d2cf68bdb2f8bd2903b7a80548c9ef26c2d9d97875b4caa9e", "sizeBytes": "376"}
    invocation = control["invocation"]
    assert invocation == "34bc596d-3a39-450a-8a29-f94ba99479f0"
    executions = rpc("GetExecution", {"executionLookup": {"invocationId": invocation, "actionDigestHash": owner["digest"]["hash"]},
                                      "inlineExecuteResponse": True}).get("execution", [])
    execution, = [e for e in executions if e.get("actionDigest", {}).get("hash") == owner["digest"]["hash"]
                  and str(e["actionDigest"]["sizeBytes"]) == "376"]
    assert execution["stage"] in (4, "COMPLETED")
    response = execution["executeResponse"]
    assert not response.get("cachedResult") and response["result"].get("exitCode") == 1
    result = response["result"]
    output = result.get("stderrDigest", {})
    data = blob(output, invocation) if int(output.get("sizeBytes", 0)) else base64.b64decode(result.get("stderrRaw", ""), validate=True)
    assert 0 < len(data) < 4096
    message = json.loads(data)["msg"]
    missing = re.search(r"executable file `?/bin/bash`? not found: No such file or directory", message)
    assert missing
    action = cas(owner["digest"], "Action")
    command = cas(action["commandDigest"], "Command")
    args = command["arguments"]
    assert args == owner["args"] + [positive["args"][1]] and owner["args"] == ["/bin/bash", "-c"]
    assert len(command["platform"]["properties"]) == len(owner["platform"])
    assert {p["name"]: p["value"] for p in command["platform"]["properties"]} == {p["name"]: p["value"] for p in owner["platform"]}
    r["control_recovery"] = {"status": "EXPECTED_FAILURE", "invocation": invocation, "action": owner["digest"],
                             "argv": args, "exit": 1, "stderr_sha256": sha256(data).hexdigest(), "stderr_bytes": len(data),
                             "message": missing.group(0), "fresh_remote": True}
    r["status"] = "PASS"


if __name__ == "__main__":
    if sys.argv[1:] == ["--recover-control"]:
        r = {"status": "INCOMPLETE", "recovery_source": os.environ["GITHUB_SHA"], "started": now()}
        save(r)
        try:
            recover_control(r)
        except Exception as exc:
            failure(r, "recovery_error", exc)
        r["verdict_time"] = now()
        save(r)
        raise SystemExit(0 if r["status"] == "PASS" else 1)
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
        r.update(status="PASS" if passed else ("FAIL" if r.get("build_exit") or r.get("preflight", {}).get("exit") else "INCOMPLETE"), verdict_time=now())
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
