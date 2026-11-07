"""Replay the host error, then qualify the test dependency barrier."""
import json
import subprocess

import build_test as q


def shared_links(arch, global_static):
    labels = ["//vdso:vdso.so", "//tools/ioctl_sniffer:ioctl_hook"]
    flags = q.opts(arch, native=False, remote=True)
    flags.remove("--spawn_strategy=remote")
    if not global_static:
        flags.remove("--features=fully_static_link")
    args = (q.startup(False, False) + ["aquery"] + flags + ["--remote_executor=", "--remote_cache=", "--bes_backend=",
            "--output=jsonproto", "--noinclude_artifacts", "--include_param_files",
            'mnemonic("CppLink", filter("//(vdso:vdso[.]so|tools/ioctl_sniffer:ioctl_hook)$", deps(//:build_test_15__deps)))'])
    p = subprocess.run(args, capture_output=True)
    result = {"exit": p.returncode, "diagnostics": q.local_detail(p.stderr.decode("utf-8", "replace")), "actions": []}
    if p.returncode == 0:
        data = json.loads(p.stdout)
        targets = {x["id"]: x["label"].removeprefix("@@") for x in data.get("targets", [])}
        configs = {x["id"]: x.get("checksum") for x in data.get("configuration", [])}
        for a in data.get("actions", []):
            if targets.get(a["targetId"]) not in labels or a["mnemonic"] != "CppLink":
                continue
            argv = a.get("arguments", []) + [arg for f in a.get("paramFiles", []) for arg in f.get("arguments", [])]
            archives = [q.clean(arg) for arg in argv if arg.endswith((".a", ".lo", ".so", ".lds"))]
            result["actions"].append({"owner": targets[a["targetId"]], "config": configs.get(a["configurationId"]),
                                      "static": "-static" in argv, "shared": "-shared" in argv, "tool": q.clean(argv[0]),
                                      "execution_platform": q.clean(a.get("executionPlatform")),
                                      "outputs": [q.clean(argv[i+1]) for i in range(len(argv)-1) if argv[i]=="-o"],
                                      "archives": archives[:80], "archive_count": len(archives),
                                      "argv_sha256": q.sha(json.dumps(argv).encode()),
                                      "argv_without_static_sha256": q.sha(json.dumps([x for x in argv if x != "-static"]).encode())})
    result["observed_requested_difference"] = (set(r["owner"] for r in result["actions"]) == set(labels) and
                                               all(r["shared"] and r["static"] == global_static for r in result["actions"]))
    q.report["cells"][f"links-{arch}-global-static-{global_static}"] = result
    q.save()


def contract():
    strategy = "linux-sandbox"
    direct = q.control("defaults", False, host_strategy=strategy)
    original = q.report["cells"]["host-defaults-direct"]
    q.report["original_host_command"] = {"matched_previous_exit": original["exit"] == 2, "strategy": strategy}
    prefix = ""
    if original["exit"] == 2 and not original["tests"] and not original["spawns"]:
        diagnostic = "\n".join(original.get("client_diagnosis", []) + original.get("finished", {}).get("diagnosis", [])).lower()
        if "linux-sandbox" in diagnostic and any(x in diagnostic for x in ("not registered", "no strategy", "not available", "not supported", "does not exist")):
            strategy = "processwrapper-sandbox"
            prefix = "fallback-"
            direct = q.control("defaults", False, host_strategy=strategy, prefix=prefix)
        else:
            q.report["original_host_command"]["different_failure"] = True
            q.save()
            return
    q.report["contract_strategy"] = strategy
    results = {"defaults": [direct]}
    results["defaults"].append(q.control("defaults", True, host_strategy=strategy, prefix=prefix))
    results["barrier"] = [q.control("barrier", suite, host_strategy=strategy, prefix=prefix) for suite in (False, True)]
    q.report["forms"] = results
    q.report["choice"] = next((form for form in results if all(results[form])), None)
    q.save()


contract()
for arch in q.ARCH:
    for global_static in (True, False):
        shared_links(arch, global_static)
q.report["verified"] = bool(q.report.get("choice"))
q.save()
if not q.report["verified"]:
    raise SystemExit("Inspect the original exit, failed input and native test separately")
