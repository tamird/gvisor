"""Observe the LLVM/GNU plugin Make rule on the native tool-free worker."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "plugin-evidence"
TARGET = "@libpluginstack//:libpluginstack"
OWNER = "@@+_repo_rules+libpluginstack//:libpluginstack"
NATIVE = "docker://gcr.io/distroless/static-debian13@sha256:d03c145fad326bff275ec4106475ed8310426caf709decd9f8cbc494af59bc18"
BASE = "f503093fe2a498edc2b786ba31db1c777505b26c"
CONFIG = "@@+_repo_rules+libpluginstack//:pmd_config"
HELPER = "@@+_repo_rules+plugin_dpdk//:pmdinfogen"
COMPILER_IMAGE = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
OLD = "6fea7f1fe69df20db5f2358d5ecdeefba46ea16303228d402e900c626a08521a/351"


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save(r):
    (OUT / "result.json").write_text(json.dumps(r, indent=2) + "\n")


def diagnostics(text):
    # Extract just tool names and fixed status phrases, never environment or
    # raw command lines from foreign_cc's failure dump.
    found = []
    patterns = [
        r"([\w./+-]{1,150}): (command not found|applet not found|No such file or directory|not found|Permission denied|cannot execute binary file)",
        r"(cannot execute): (required file not found)",
        r"([\w./+-]{1,120}): (unrecognized option|invalid option|unknown option)",
        r"([\w./+-]{1,120}): (invalid applet|not supported)",
        r"(fatal error): ([\w./+-]{1,120}: No such file or directory)",
        r"(fatal error): ('[A-Za-z0-9_./+-]+' file not found)",
        r"(error): ((?:call to |use of )?undeclared (?:function|identifier) '[A-Za-z_][A-Za-z0-9_]*')",
        r"(undefined symbol): ([A-Za-z_][A-Za-z0-9_]*)",
        r"(error): (unknown type name [\w']{1,60})",
        r"(No rule to make target) [‘'\x60]([\w./+-]{1,150})",
        r"(Makefile|[\w./+-]{1,120}\.mk):(\d+: [\w./+-]{1,100})",
        r"(BUILD|MODULE\.bazel|framework\.bzl):(\d+:\d+): ",
        r"(error applying patch|Checksum was|no such package|no such target|Unable to find package|does not exist|invalid label|syntax error|Failed to load Starlark extension|Error in repository |inconsistent exit status)()",
    ]
    for line in text.splitlines():
        if re.search(r"authorization|api[-_]?key|remote_header|bes_header|token|credential|https?://", line, re.I):
            continue
        for regex in patterns:
            for match in re.finditer(regex, line, re.I):
                parts = [part for part in match.groups() if part]
                message = " ".join(parts)
                if message not in found and len(found) < 60:
                    found.append(message)
    stages = ["Building libpluginstack", "generating custom config", "Build complete",
              "Foreign Cc Rules for Make", "ERROR: BUILD FAILED", "building dpdk"]
    return {"observed_stages": [s for s in stages if s.lower() in text.lower()],
            "diagnostics": found}


def audit(compact, include_links=False):
    query = """select(.spawn != null) | .spawn | select(.targetLabel == $owner or .targetLabel == $config or .targetLabel == $helper or ($include_links and .mnemonic == "GoLink") or (.exitCode != null and .exitCode != 0))
        | {mnemonic, targetLabel, args:(if .targetLabel == $owner or .targetLabel == $config or .targetLabel == $helper then .args[0:2] else [] end), digest, runner, cacheHit, exitCode, status,
        target_triples: ([range(0; (.args | length)) as $i | select(.args[$i] == "-target" or .args[$i] == "--target") | .args[$i+1]] + [.args[]? | select(startswith("--target=")) | ltrimstr("--target=")]),
        output: (if .mnemonic == "GoLink" then (.args | index("-o")) as $i | if $i == null then null else .args[$i+1] end else null end),
        platform:[.platform.properties[]?|select(.name=="Arch" or .name=="OSFamily" or
               .name=="container-image" or .name=="workload-isolation-type" or .name=="recycle-runner")]}"""
    with subprocess.Popen([str(TMP / "bb"), "print", "--raw", "--compact_execution_log=" + str(compact)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as reader:
        rows = subprocess.run(["jq", "-c", "--arg", "owner", OWNER, "--arg", "config", CONFIG, "--arg", "helper", HELPER, "--argjson", "include_links", json.dumps(include_links), query], stdin=reader.stdout,
                              stdout=subprocess.PIPE, text=True, check=True)
        reader.stdout.close()
        assert reader.wait() == 0
    return [json.loads(line) for line in rows.stdout.splitlines()]


def inspect_outputs(outputs):
    r = {}
    binary = next(iter(outputs["dpdk-pmdinfogen"]))
    binary_data = binary.read_bytes()
    header = subprocess.check_output(["readelf", "-h", binary], text=True)
    segments = subprocess.check_output(["readelf", "-l", binary], text=True)
    dynamic = subprocess.check_output(["readelf", "-d", binary], text=True)
    assert binary_data.startswith(b"\x7fELF") and "Advanced Micro Devices X86-64" in header
    assert "INTERP" not in segments and "(NEEDED)" not in dynamic
    r["helper"] = {"sha256": hashlib.sha256(binary_data).hexdigest(), "bytes": len(binary_data),
                   "machine": "x86_64", "interpreter": None, "needed": []}
    config = next(iter(outputs["rte_config.h"])).read_bytes()
    assert b"#define RTE_ARCH_64" in config and b"#define RTE_ARCH_X86_64" in config
    r["configuration"] = {"sha256": hashlib.sha256(config).hexdigest(), "bytes": len(config),
                          "helper_matches_plugin": True}
    archives = outputs["libpluginstack.a"]
    assert len(archives) == 1
    path, = archives
    data = path.read_bytes()
    assert data.startswith(b"!<arch>\n")
    symbols = subprocess.check_output(["nm", "--defined-only", "--extern-only", path], text=True,
                                      stderr=subprocess.DEVNULL)
    required = ["plugin_socket", "plugin_epoll_wait", "plugin_get_port_range"]
    assert all(re.search(r"\bT " + name + r"$", symbols, re.M) for name in required)
    pmd_symbols = re.findall(r"\b[RD] (\w+_pmd_info)$", symbols, re.M)
    assert pmd_symbols
    r["archive"] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                    "defined": required, "pmd_info_symbols": sorted(set(pmd_symbols))}
    return r


def main():
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    r = {"source": head, "base": BASE, "target": TARGET, "old_invocation": "9125942b-1fc2-42df-b235-6dee4e0ae37a",
         "old_action": OLD, "old_launch": "/bin/bash", "old_exit": 1, "worker": NATIVE,
         "previous_analysis_failure": {
             "invocation": "d2d7b470-a4a5-47ec-ab0e-79c4f3486bf4",
             "artifact": 13106028221, "exit": 1, "processes": 0,
             "cause": "kwargs unpacking is forbidden in BUILD files"},
         "previous_helper_failure": {"invocation": "f9a06f1c-cc5f-4b9c-b518-128f1505231b",
                                     "action": "28ae53bea672c865eada319c9fb0c64f84085a01d2779651eb8fa3191be9c5de/376",
                                     "exit": 2, "helper": "gen-config-h.sh"},
         "previous_binutils_failure": {
             "artifact": 13106596549,
             "invocation": "79134506-3dfa-4729-b351-cc70f2abe4d1",
             "action": "f4a5337c96cede88ef5eb5ac42e3e9bb09b1b313526a5200f477ca7ef77a5847/376",
             "exit": 2, "cause": "unpopulated legacy LLVM ld and objdump paths"},
         "declared_toolchains": {
             "make": "GNU Make 4.4.1 (BCR 4.4.1.bcr.4)",
             "plugin_compiler": "hermetic-llvm 0.8.24 Clang targeting GNU libc on Linux AMD64",
             "plugin_binutils": ["@llvm//tools:ld.lld", "@llvm//tools:llvm-objdump"],
             "helper_compiler": "hermetic-llvm 0.8.24 Clang targeting musl on native Linux AMD64",
             "scope": "This integration does not qualify public #15153's GCC toolchain."},
         "expected": {"target": TARGET, "support": [CONFIG, HELPER]},
         "started": now(), "status": "INCOMPLETE"}
    save(r)
    try:
        assert head == os.environ["GITHUB_SHA"]
        assert subprocess.check_output(["git", "rev-parse", "HEAD^"], text=True).strip() == BASE
        assert subprocess.run(["git", "diff", "--quiet", "HEAD", "--"]).returncode == 0
        assert json.loads((OUT / "image.json").read_text())["manifest"] == NATIVE.split("@")[1]
        bazel = ["bazel", "--bazelrc=" + str(TMP / "sidecar.bazelrc")]
        plat = "//test/rbe/empty_worker:amd64_musl"
        bep = TMP / "plugin.bep"
        compact = TMP / "plugin.compact"
        flags = ["--config=x86_64", "--config=plugin-tldk", "-c", "opt", "--host_platform=" + plat,
                 "--extra_execution_platforms=" + plat, "--remote_accept_cached",
                 "--noremote_upload_local_results", "--jobs=400", "--remote_download_outputs=minimal",
                 "--remote_download_regex=(^|/)(libpluginstack[.]a|dpdk-pmdinfogen|rte_config[.]h)$", "--noremote_local_fallback",
                 "--spawn_strategy=remote", "--color=no", "--curses=no",
                 "--registry=" + (Path(os.environ["GITHUB_WORKSPACE"]) / "registry").as_uri(),
                 "--registry=https://bcr.bazel.build", "--build_event_json_file=" + str(bep),
                 "--build_event_json_file_path_conversion=false", "--execution_log_compact_file=" + str(compact)]
        log = TMP / "plugin.log"
        with log.open("w") as dst:
            rc = subprocess.run(["timeout", "--signal=INT", "--kill-after=25s", "38m",
                                 *bazel, "build", *flags, TARGET], stdout=dst, stderr=subprocess.STDOUT).returncode
        r.update(build_exit=rc, build_status="FAIL" if rc else "PASS", build_finished=now(), status="FAIL" if rc else "INCOMPLETE")
        save(r)
        r.update(diagnostics(log.read_text(errors="replace")))
        save(r)
        file_sets = {}
        roots = []
        for line in bep.open():
            event = json.loads(line)
            if "started" in event:
                r["invocation"] = event["started"]["uuid"]
            if "buildFinished" in event["id"]:
                finished = event["finished"]
                r["bazel_finished"] = {k: finished.get(k) for k in ("exitCode", "finishTime")}
            if "targetCompleted" in event["id"] and event["id"]["targetCompleted"]["label"] in (TARGET, OWNER) and "aspect" not in event["id"]["targetCompleted"]:
                r["target_completed"] = bool(event.get("completed", {}).get("success"))
                roots = [f["id"] for group in event.get("completed", {}).get("outputGroup", [])
                         if group["name"] == "default" for f in group.get("fileSets", [])]
            if "namedSetOfFiles" in event:
                file_sets[event["id"]["namedSet"]["id"]] = event["namedSetOfFiles"]
        save(r)
        actions = audit(compact)
        r["actions"] = actions
        save(r)
        make_actions = [a for a in actions if a["targetLabel"] == OWNER and a["mnemonic"] == "CcMakeMakeRule"]
        if rc == 0 or make_actions:
            assert len(make_actions) == 1
            action, = make_actions
            props = {item["name"]: item["value"] for item in action["platform"]}
            assert props["container-image"] == NATIVE and props["Arch"] == "amd64"
            assert props["recycle-runner"] == "false" and props["workload-isolation-type"] == "oci"
            assert action["runner"] == "remote" and not action["cacheHit"] and action["args"][0].endswith("plugin_programs/bin/bash")
            assert action["args"][1].endswith("wrapper_build_script.sh")
            r["verified_launch"] = True
        if rc == 0:
            assert not action["exitCode"] and r["target_completed"] and roots
            files = []
            visited = set()
            while roots:
                node = roots.pop()
                if node in visited:
                    continue
                visited.add(node)
                group = file_sets[node]
                files.extend(f for f in group.get("files", []) if f["name"].rsplit("/", 1)[-1]
                             in ("libpluginstack.a", "dpdk-pmdinfogen", "rte_config.h"))
                roots.extend(f["id"] for f in group.get("fileSets", []))
            from urllib.parse import unquote, urlparse
            assert all(urlparse(f["uri"]).scheme == "file" for f in files)
            outputs = {}
            for file in files:
                name = file["name"].rsplit("/", 1)[-1]
                path = Path(unquote(urlparse(file["uri"]).path))
                outputs.setdefault(name, set()).add(path)
            assert set(outputs) == {"libpluginstack.a", "dpdk-pmdinfogen", "rte_config.h"}
            assert all(len(paths) == 1 for paths in outputs.values())
            support = [a for a in actions if a["targetLabel"] in (CONFIG, HELPER)]
            assert {a["mnemonic"] for a in support if a["targetLabel"] == HELPER} >= {"CppCompile", "CppLink"}
            assert any(a["targetLabel"] == CONFIG and a["mnemonic"] == "CcMakeMakeRule" for a in support)
            for a in support:
                properties = {v["name"]: v["value"] for v in a["platform"]}
                assert properties["Arch"] == "amd64" and properties["OSFamily"] == "linux"
                assert properties["container-image"] == (NATIVE if a["targetLabel"] == CONFIG else COMPILER_IMAGE)
                assert not a["exitCode"] and (a["runner"] == "remote" or a["cacheHit"])
            helper_compiles = [a for a in support if a["targetLabel"] == HELPER and a["mnemonic"] == "CppCompile"]
            assert helper_compiles and all(a["args"][0].endswith("/clang") and
                                          a["target_triples"] == ["x86_64-linux-musl"] for a in helper_compiles)
            r["support_disposition"] = {"fresh_remote": sum(not a["cacheHit"] for a in support),
                                       "cached": sum(bool(a["cacheHit"]) for a in support)}
            r.update(inspect_outputs(outputs))
            r["status"] = "PASS"
    except Exception as exc:
        import traceback
        r["collection_error"] = {"type": type(exc).__name__, "line": traceback.extract_tb(exc.__traceback__)[-1].lineno}
    r["source_clean"] = subprocess.run(["git", "diff", "--quiet", "HEAD", "--"]).returncode == 0
    if not r["source_clean"] and r["status"] == "PASS":
        r["status"] = "INCOMPLETE"
    r["verdict_time"] = now()
    save(r)
    return 0 if r["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
