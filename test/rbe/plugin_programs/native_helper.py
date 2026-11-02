"""Compare host and declared launchers for the real native musl DPDK build."""
import base64
from collections import Counter
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback
import uuid
from zipfile import ZipFile

from action_log import context
from qualify import CONFIG, HELPER, NATIVE, OUT, OWNER, TARGET, TMP, audit, diagnostics, inspect_outputs, now, save

PRODUCT = "6eee103ac90c410b75f5a4390a423bf5f622acb2"
BASE = "cfa98382ba656459e79cfbece6a2c533cf47fe59"
ORIGINAL = "c1210292-5868-4f9a-9ed1-ad1d49308b27"
OLD_ARCHIVE = {"hash": "c31a5dc008783cf4d2f1ad4fcff830adf642a2ba2121c438d4ca6509dd126440", "sizeBytes": "7403456"}
OLD_CONFIG = "8fab57f51b67b2d8ac0aedd2988d3ad61173071fa6625ba23fb60a4b47838597"
PMD = ["net_ring_pmd_info", "net_virtio_pmd_info", "net_virtio_user_pmd_info"]
VFIO = ["pci_vfio_read_config", "pci_vfio_map_resource", "rte_vfio_container_dma_map"]


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def check_selection(r, bazel, flags, platform, control):
    query = 'mnemonic("CcMakeMakeRule", set(@libpluginstack//:libpluginstack @libpluginstack//:pmd_config))'
    invocation = str(uuid.uuid4())
    q = subprocess.run([*bazel, "aquery", *flags, "--invocation_id=" + invocation, "--output=jsonproto", "--noinclude_artifacts", query],
                       text=True, capture_output=True, timeout=420)
    selection = r["selection"] = {"invocation": invocation, "exit": q.returncode, "verified": False}
    save(r)
    if q.returncode:
        selection["diagnostic"] = context(q.stderr)
        save(r)
        raise ValueError("Could not analyze the selected tools")
    data = json.loads(q.stdout)
    targets = {t["id"]: t["label"] for t in data["targets"]}
    rows = []
    for a in data["actions"]:
        argv = a["arguments"]
        rows.append({"target": targets[a["targetId"]], "argv": argv,
                     "exec_platform": a["executionPlatform"], "aquery_key": a["actionKey"]})
    selection["actions"] = rows
    save(r)
    assert len(rows) == 2 and {a["target"].rsplit(":", 1)[1] for a in rows} == {"libpluginstack", "pmd_config"}
    for a in rows:
        args = a["argv"]
        assert a["target"].lstrip("@").split("//")[0] in ("libpluginstack", "+_repo_rules+libpluginstack")
        assert a["exec_platform"].lstrip("@") == platform and args[-1].endswith("/wrapper_build_script.sh")
        if control:
            assert len(args) == 3 and args[:2] == ["/bin/bash", "-c"]
        else:
            assert len(args) == 2 and args[0].endswith("plugin_programs/bin/bash")
    selection["verified"] = True
    save(r)


def build(r, control=False, analyze_only=False):
    r.update(source=git("rev-parse", "HEAD"), product=PRODUCT, base=BASE, target=TARGET, started=now(),
             original={"invocation": ORIGINAL, "run": 41406257977, "artifact": 13123707083, "archive": OLD_ARCHIVE},
             arm="host-shell" if control else "declared-shell")
    save(r)
    assert r["source"] == os.environ["GITHUB_SHA"] and git("rev-parse", "HEAD^") == PRODUCT
    assert git("rev-parse", PRODUCT+"^") == BASE
    assert set(git("diff", "--name-only", BASE, PRODUCT).splitlines()) == {
        "MODULE.bazel", "external/tools/plugin-stack/BUILD", "external/tools/plugin-stack/plugin-stack.BUILD",
        "test/rbe/plugin_programs/framework.patch"}
    assert set(git("diff", "--name-only", PRODUCT, "HEAD").splitlines()) == {
        ".github/workflows/build.yml", "test/rbe/plugin_programs/native_helper.py"}
    assert git("diff", "--name-only", "HEAD") == ("MODULE.bazel" if control else "")
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
    check_selection(r, bazel, flags, platform, control)
    if analyze_only:
        return
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
        # Bazel's rendered log can contain JSON-escaped provider stderr. Verify
        # the original structured stderr below, when the RPC key is in scope.
        assert rc and owner["args"][0] == "/bin/bash" and (owner["exitCode"] or owner["status"])
        r["pending_control_stderr"] = True
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
    save(r)
    assert proof["outputs"] == original_result()["original"]["proof"]["outputs"]
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
    assert proof["byte_identical_archive"] and proof["new_api"] == proof["old_api"]
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
    proof["verified"] = True
    save(r)


def failure(r, key, exc):
    r[key] = {"type": type(exc).__name__, "line": traceback.extract_tb(exc.__traceback__)[-1].lineno}
    save(r)


def original_result():
    archive = TMP / "original.zip"
    assert sha256(archive.read_bytes()).hexdigest() == "4e77036c8b4704ab3b14e57a646a146a65960043ba502ee47cd3d590b9d34848"
    with ZipFile(archive) as z:
        original = json.loads(z.read("result.json"))
    assert original["status"] == "PASS" and original["recovery_source"] == BASE
    assert original["original_run"] == 41421129869
    return original


def verify_missing_bash(r):
    from cas_inputs import blob, cas, rpc

    control = r["control"]
    assert control["selection"]["verified"]
    original = original_result()
    if control.get("pending_control_stderr"):
        negative = control
    else:
        negative = original["original"]["control"]
        control["evidence"] = {"source": negative["source"], "run": 41422486998, "artifact": 13130935501}
    action, = [a for a in negative["actions"] if a["targetLabel"] == OWNER and a["mnemonic"] == "CcMakeMakeRule"]
    assert action["runner"] == "remote" and not action["cacheHit"] and negative["build_exit"] == 1
    digest, invocation = action["digest"], negative["invocation"]
    response, = [e["executeResponse"] for e in rpc("GetExecution", {"executionLookup": {
        "invocationId": invocation, "actionDigestHash": digest["hash"]}, "inlineExecuteResponse": True}).get("execution", [])
                 if e.get("actionDigest", {}).get("hash") == digest["hash"]
                 and str(e["actionDigest"]["sizeBytes"]) == str(digest["sizeBytes"]) and e["stage"] in (4, "COMPLETED")]
    assert not response.get("cachedResult") and response["result"]["exitCode"] == 1
    result = response["result"]
    output = result.get("stderrDigest", {})
    data = blob(output, invocation) if int(output.get("sizeBytes", 0)) else base64.b64decode(result["stderrRaw"], validate=True)
    assert 0 < len(data) < 4096
    message = re.search(r"executable file `?/bin/bash`? not found: No such file or directory", json.loads(data)["msg"])
    assert message, "unexpected original control diagnostic"
    command = cas(cas(digest, "Action")["commandDigest"], "Command")
    (a,) = [a for a in control["selection"]["actions"] if a["target"].endswith("//:libpluginstack")]
    assert command["arguments"] == a["argv"]
    props = {p["name"]: p["value"] for p in command["platform"]["properties"]}
    assert props == {"Arch":"amd64", "OSFamily":"linux", "container-image":NATIVE, "recycle-runner":"false",
                     "workload-isolation-type":"oci"}
    control.update(build_verified=True, disposition="EXPECTED_FAILURE", invocation=invocation,
                   action=digest, stderr_sha256=sha256(data).hexdigest(), message=message.group(0))
    if not control.get("pending_control_stderr"):
        old = original["control_recovery"]
        assert [control[k] for k in ("action","invocation","stderr_sha256","message")] == [
            old[k] for k in ("action","invocation","stderr_sha256","message")]
    save(r)


if __name__ == "__main__":
    if sys.argv[1:] == ["--outputs"]:
        r = json.loads((OUT / "result.json").read_text())
        checks = [("control_error", verify_missing_bash)]
        if r.get("build_verified"):
            checks.extend([("output_error", collect), ("launch_error", launch_inputs)])
        for key, operation in checks:
            try:
                operation(r)
            except Exception as exc:
                failure(r, key, exc)
        passed = all((r.get("source_clean"),r.get("build_verified"),r.get("selection",{}).get("verified"),
                      r.get("proof",{}).get("verified"),r.get("launch",{}).get("verified"),
                      r.get("control",{}).get("build_verified"))) and not any(k in r for k, _ in checks)
        status = "PASS" if passed else "FAIL" if r.get("build_exit", 0) != 0 else "INCOMPLETE"
        r.update(status=status, verdict_time=now())
        save(r)
        raise SystemExit(0 if passed else 1)

    assert sys.argv[1:] in ([], ["--fresh-control"])
    path = Path("MODULE.bazel")
    original = path.read_bytes()
    needle = b'register_toolchains("//external/tools/plugin-stack:linux_amd64_bash")\n'
    assert original.count(needle) == 1
    control = {"status": "INCOMPLETE"}
    try:
        path.write_bytes(original.replace(needle, b""))
        build(control, control=True, analyze_only=not sys.argv[1:])
    except Exception as exc:
        failure(control, "collection_error", exc)
    finally:
        path.write_bytes(original)
    r = {"status":"INCOMPLETE", "control":control}
    save(r)
    try:
        build(r)
    except Exception as exc:
        failure(r, "collection_error", exc)
    finally:
        r["source_clean"] = not git("diff", "--name-only", "HEAD")
        save(r)
    raise SystemExit(0 if r.get("build_verified") and r["source_clean"] else 1)
