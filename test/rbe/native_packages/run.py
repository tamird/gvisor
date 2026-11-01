"""Qualify the ordinary native syscall entry point on the proposed source-built static-glibc platforms."""

import collections
import datetime
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys

import qualification_report as report
from static_build import elf


SOURCE = os.environ["PRODUCT"]
IMAGE = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
TARGETS = ["//test/syscalls:fork_test_native"]
CASES = {TARGETS[0]: ["ForkTest/Simple", "ForkTest/Pipe", "ForkTest/SharedMapping"]}
LINK_TARGETS = ["//test/syscalls/linux:fork_test", "//test/runner/setup_container:setup_container"]
ABI = {"selected": "static glibc 2.44 on both native scratch execution architectures",
       "supplier_proposal": "https://github.com/hermeticbuild/hermetic-llvm/pull/768",
       "supplier_source": "2aabeae31470b4ac062918e53fc109859b6fca88",
       "supplier_base": "llvm 0.8.24",
       "supplier_delta": "First four proposal commits, excluding unrelated libstdc++, plus action-local compiler temporary files",
       "prior_static_GNU_attempt": {"run": 41308187475, "artifact": 13084852008, "status": "FAIL", "executed_cases": 0},
       "GNU_boundary_proof": {"run": 41302038018, "artifact": 13082191714},
       "launcher_proposal": "8919cb68b11643114c1f28c1a756902a2958bda0",
       "scope": "Ordinary native entry point and proposed GNU static platform, no run_under or per-target change",
       "limitation": "Static glibc 2.44 is distinct from the default dynamic GNU target. These cases do not qualify dlopen, external NSS modules, or locale data"}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args])


def original_tests(tests, targets, wrapper="compiled_posix_test_wrapper"):
    if collections.Counter(t["target"] for t in tests) != collections.Counter(targets):
        return False
    for test in tests:
        package, name = test["target"].removeprefix("//").split(":")
        if (test["runner"] != "remote" or test["cache_hit"] or test["status"] or test["exit_code"]
                or Path(test["executable"]).name != wrapper
                or not test["test_binary"].startswith(package + "/")
                or Path(test["test_binary"]).name != name):
            return False
    return True


def native_actions(path, arch, tmp):
    """Reuse the qualified raw-spawn projection, retaining each test action."""
    # c37bd3473:test/rbe/empty_worker/wildcard.py; raw/expanded parity is
    # recorded in Actions 41282902598, artifact 13074631555.
    projection = 'select(has("spawn")) | .spawn | {m:.mnemonic,l:.targetLabel,x:.args[0],t:(if .mnemonic == "TestRunner" then .args[1:] else null end),r:.runner,c:.cacheHit,s:.status,e:.exitCode,d:.digest,p:(.platform.properties // []),v:[.envVars[]? | select(.name == "GVISOR_SYSCALL_TEST_BINARY" or .name == "GVISOR_SYSCALL_TEST_RUNNER")]}'
    counts = collections.Counter()
    tests = []
    bad = []
    glibc_actions = collections.Counter()
    with subprocess.Popen([str(tmp / "bb"), "print", "--raw", "--compact_execution_log=" + str(path)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as decoder:
        with subprocess.Popen(["jq", "-c", projection], stdin=decoder.stdout,
                              stdout=subprocess.PIPE, text=True) as projected:
            decoder.stdout.close()
            for line in projected.stdout:
                row = json.loads(line)
                props = {p["name"]: p["value"] for p in row["p"]}
                platform = {key: props.get(key) for key in ("Arch", "container-image", "workload-isolation-type")}
                correct = platform == {"Arch": arch, "container-image": IMAGE, "workload-isolation-type": "oci"}
                runner = row["r"] or "unknown"
                counts["total"] += 1
                counts["off_platform"] += not correct
                counts["local"] += not row["c"] and runner != "remote"
                counts["failed"] += bool(row["s"] or row["e"])
                if row["m"].startswith("GlibcStatic"):
                    glibc_actions[row["m"] + (":cached" if row["c"] else ":" + runner)] += 1
                item = {"target": row["l"], "mnemonic": row["m"], "executable": row["x"],
                        "runner": runner, "cache_hit": bool(row["c"]), "platform": platform,
                        "status": row["s"], "exit_code": row["e"], "digest": row["d"]}
                if row["m"] == "TestRunner":
                    item["arguments"] = row["t"]
                    item["runfile_environment"] = {value["name"]: value["value"] for value in row["v"]}
                    item["test_binary"] = row["t"][0] if row["t"] else ""
                    tests.append(item)
                if (not correct or runner not in ("remote", "remote cache hit") or (row["m"] != "TestRunner" and (row["s"] or row["e"]))) and len(bad) < 30:
                    bad.append(item)
            projection_exit = projected.wait()
        decode_exit = decoder.wait()
    original = [test for test in tests if test["test_binary"] != "--generate-xml"]
    auxiliary = [test for test in tests if test["test_binary"] == "--generate-xml"]
    valid_tests = collections.Counter(test["target"] for test in original) == collections.Counter(TARGETS)
    wrapper = "posix_tw" if arch == "amd64" else "compiled_posix_test_wrapper"
    for test in original:
        arguments = test["arguments"]
        valid_tests &= (test["runner"] == "remote" and not test["cache_hit"]
                        and Path(test["executable"]).name == wrapper
                        and test["test_binary"] == "test/syscalls/fork_test_native")
        valid_tests &= (arguments[1:2] == ["--syscall-test"] and "--platform=native" in arguments
                        and test["runfile_environment"] == {
                            "GVISOR_SYSCALL_TEST_BINARY": "test/syscalls/linux/fork_test",
                            "GVISOR_SYSCALL_TEST_RUNNER": "test/runner/runner_/runner"})
    return {"status": "PASS" if counts["total"] and not counts["failed"] and not bad and not decode_exit and not projection_exit and valid_tests else "INCOMPLETE",
            "counts": dict(counts), "glibc_actions": dict(glibc_actions), "decoder_exits": [decode_exit, projection_exit],
            "tests": original, "xml_actions": auxiliary, "failed_or_other": bad}


def product_files(bazel, common, work, execroot, arch):
    targets = [*TARGETS, *LINK_TARGETS]
    with (work / "output-query.log").open("w") as log:
        result = subprocess.run(["timeout", "--signal=INT", "--kill-after=25s", "8m",
                                 *bazel, "cquery", *common, "--output=files", "set(" + " ".join(targets) + ")"],
                                stdout=subprocess.PIPE, stderr=log, text=True)
    if result.returncode:
        return {"status": "INCOMPLETE", "native_exit": result.returncode}
    outputs = {}
    for target in targets:
        name = target.split(":")[1]
        files = [line for line in result.stdout.splitlines() if Path(line).name == name]
        if len(files) != 1 or not (execroot / files[0]).is_file():
            return {"status": "INCOMPLETE", "outputs": outputs,
                    "reason": "The original executable was not materialized locally", "target": target}
        content = (execroot / files[0]).read_bytes()
        data = {"path": files[0], "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        data.update(kind="ELF", **elf(content))
        data["status"] = "PASS" if (data["machine"] == {"amd64": 62, "arm64": 183}[arch]
                                    and not data["interpreter"] and not data["needed"]) else "FAIL"
        outputs[target] = data
    if outputs[TARGETS[0]]["sha256"] != outputs[targets[-1]]["sha256"]:
        return {"status": "FAIL", "outputs": outputs, "reason": "The native launcher differs from setup_container"}
    return {"status": "PASS" if all(v["status"] == "PASS" for v in outputs.values()) else "FAIL", "outputs": outputs}


def failure_lines(reports):
    lines = []
    for path in reports.rglob("test.log"):
        for line in path.read_text(errors="replace").splitlines():
            if (re.search(r"execv[pel]*:|execv[pel]*\(|no such file|permission denied|exec format|cannot execute|fork/exec|^\[ *(RUN|FAILED|PASSED)", line, re.I)
                    and not re.search(r"api.key|remote.header|bes.header|authorization|token=|credential", line, re.I)):
                lines.append(line[:1200])
    return lines[:30]


def static_links(graph):
    """Require both link actions to consume the supplier's static runtimes."""
    fragments = {str(f["id"]): f for f in graph["pathFragments"]}
    def path(fragment_id):
        fragment = fragments[str(fragment_id)]
        parent = fragment.get("parentId")
        return (path(parent) + "/" if parent else "") + fragment["label"]
    artifacts = {str(a["id"]): path(a["pathFragmentId"]) for a in graph["artifacts"]}
    deps = {str(d["id"]): d for d in graph["depSetOfFiles"]}
    targets = {str(t["id"]): t["label"] for t in graph["targets"]}
    def input_paths(action):
        seen, inputs = set(), set()
        pending = list(action["inputDepSetIds"])
        while pending:
            key = str(pending.pop())
            if key in seen:
                continue
            seen.add(key)
            dep = deps[key]
            inputs.update(artifacts[str(i)] for i in dep.get("directArtifactIds", []))
            pending.extend(dep.get("transitiveDepSetIds", []))
        return inputs
    producers = {artifacts[str(output)]: action for action in graph["actions"] for output in action["outputIds"]}
    links = []
    for action in graph["actions"]:
        if action["mnemonic"] != "CppLink":
            continue
        target = targets[str(action["targetId"])]
        if target not in LINK_TARGETS:
            continue
        params = {p["execPath"]: p["arguments"] for p in action.get("paramFiles", [])}
        args = [value for arg in action["arguments"]
                for value in (params[arg[1:]] if arg.startswith("@") else [arg])]
        inputs = input_paths(action)
        archives = {}
        for name in ("libcxx", "libcxxabi", "libunwind"):
            selected = [arg for arg in args if "/" + name + "/" in arg and arg.endswith(".a") and arg in inputs]
            if len(selected) != 1:
                raise ValueError(target + ": link does not select one declared " + name)
            archives[name] = selected[0]
        search = [arg[2:] for arg in args if arg.startswith("-L") and len(arg) > 2]
        search.extend(args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "-L")
        # Both search trees must contain the proposed source-built runtimes,
        # rather than just the existing shared stubs or libc_nonshared.a.
        directories = [name for name in search if name in inputs and name in producers
                       and targets[str(producers[name]["targetId"])].endswith("//runtimes/glibc:glibc_library_search_directory")]
        if len(directories) != 1:
            raise ValueError(target + ": GNU library search directory is not a declared, produced input")
        source_archives = {}
        for name in ("libc.a", "libm.a"):
            selected = [value for value in input_paths(producers[directories[0]]) if Path(value).name == name]
            if len(selected) != 1 or selected[0] not in producers:
                raise ValueError(target + ": selected GNU directory does not consume one produced " + name)
            producer = producers[selected[0]]
            owner = targets[str(producer["targetId"])]
            if (producer["mnemonic"] != "GlibcStaticArchive"
                    or not re.search(r"//3rd_party/libc/glibc/static:glibc_static_(x86_64|aarch64)$", owner)):
                raise ValueError(target + ": " + name + " is not the proposed source-built GNU archive")
            objects = [value for value in input_paths(producer)
                       if "/3rd_party/libc/glibc/static/" in value and value.endswith(".o")]
            if not objects:
                raise ValueError(target + ": " + name + " has no declared glibc objects")
            archives[name] = selected[0]
            source_archives[name] = {"owner": owner, "object_count": len(objects)}
        if len({value["owner"] for value in source_archives.values()}) != 1:
            raise ValueError(target + ": libc and libm have different source producers")
        prefixes = [arg[2:] for arg in args if arg.startswith("-B") and len(arg) > 2]
        crt_directories = [name for name in prefixes if name in inputs and name in producers
                           and targets[str(producers[name]["targetId"])].endswith("//runtimes:crt_objects_directory_linux")]
        if len(crt_directories) != 1:
            raise ValueError(target + ": static startup directory is not declared")
        startup = {}
        for name in ("crt1.o", "rcrt1.o", "crti.o", "crtn.o"):
            selected = [value for value in input_paths(producers[crt_directories[0]]) if Path(value).name == name]
            if len(selected) != 1 or selected[0] not in producers:
                raise ValueError(target + ": missing source-built startup file " + name)
            producer = producers[selected[0]]
            if (producer["mnemonic"] != "GlibcStaticCrt"
                    or targets[str(producer["targetId"])] != source_archives["libc.a"]["owner"]):
                raise ValueError(target + ": startup file has a different libc producer")
            startup[name] = selected[0]
        static = [arg for arg in args if arg in ("-static", "-static-pie")]
        shared = [arg for arg in args if re.search(r"\.so(?:\.[0-9]+)*$", arg)]
        if not static or shared:
            raise ValueError(target + ": link is not static or still selects a shared object")
        links.append({"target": target, "action_key": action["actionKey"],
                      "archives": archives, "libc_search_directory": directories[0], "static_options": static,
                      "source_archives": source_archives, "startup_files": startup,
                      "crt_search_directory": crt_directories[0],
                      "execution_platform": action["executionPlatform"]})
    if collections.Counter(link["target"] for link in links) != collections.Counter(LINK_TARGETS):
        raise ValueError("The graph does not have exactly one link action for each original test")
    return {"status": "PASS", "links": links}


def static_link_controls():
    """Exercise the graph predicate before either remote compilation."""
    base = "external/llvm/3rd_party/libc/glibc/static/glibc_static_x86_64/"
    paths = ["external/llvm/libcxx/liblibcxx.static.a", "external/llvm/libcxxabi/liblibcxxabi.static.a",
             "external/llvm/libunwind/liblibunwind.static.a", "external/llvm/glibc_library_search_directory",
             "external/llvm/crt_objects_directory_linux", base + "lib/libc.a", base + "lib/libm.a",
             *[base + "crt/" + name for name in ("crt1.o", "rcrt1.o", "crti.o", "crtn.o")],
             base + "c.o", base + "m.o", "test/syscalls/linux/fork_test", "test/runner/setup_container/setup_container"]
    fragments, ids, artifacts = [], {}, []
    for index, value in enumerate(paths, 1):
        parent, prefix = 0, ""
        for part in value.split("/"):
            prefix += "/" + part
            if prefix not in ids:
                ids[prefix] = len(ids) + 1
                fragments.append({"id": ids[prefix], "label": part, "parentId": parent})
            parent = ids[prefix]
        artifacts.append({"id": index, "pathFragmentId": parent, "isTreeArtifact": index in (4, 5)})
    args = ["-static", "-L" + paths[3], "-B" + paths[4], *paths[:3]]
    graph = {"pathFragments": fragments, "artifacts": artifacts,
             "targets": [{"id": i + 1, "label": target} for i, target in enumerate(LINK_TARGETS)]
                        + [{"id": 3, "label": "@@llvm+//runtimes/glibc:glibc_library_search_directory"},
                           {"id": 4, "label": "@@llvm+//runtimes:crt_objects_directory_linux"},
                           {"id": 5, "label": "@@llvm+//3rd_party/libc/glibc/static:glibc_static_x86_64"}],
             "depSetOfFiles": [{"id": 1, "directArtifactIds": [1, 2, 3, 4, 5]},
                               {"id": 2, "directArtifactIds": [6, 7]},
                               {"id": 3, "directArtifactIds": [8, 9, 10, 11]},
                               {"id": 4, "directArtifactIds": [12]},
                               {"id": 5, "directArtifactIds": [13]}],
             "actions": [{"mnemonic": "CppLink", "targetId": i + 1, "actionKey": str(i),
                          "arguments": ["clang", "@params"], "paramFiles": [{"execPath": "params", "arguments": list(args)}],
                          "inputDepSetIds": [1], "outputIds": [i + 14], "executionPlatform": "//fixture:native"}
                         for i in range(2)]
                        + [{"mnemonic": "CopyToDirectory", "targetId": 3, "inputDepSetIds": [2], "outputIds": [4]},
                           {"mnemonic": "CopyToDirectory", "targetId": 4, "inputDepSetIds": [3], "outputIds": [5]},
                           {"mnemonic": "GlibcStaticArchive", "targetId": 5, "inputDepSetIds": [4], "outputIds": [6]},
                           {"mnemonic": "GlibcStaticArchive", "targetId": 5, "inputDepSetIds": [5], "outputIds": [7]}]
                        + [{"mnemonic": "GlibcStaticCrt", "targetId": 5, "inputDepSetIds": [4], "outputIds": [i]}
                           for i in (8, 9, 10, 11)]}
    static_links(graph)
    mutations = ("dynamic", "shared", "undeclared_archive", "missing_libc", "missing_owner",
                 "uncompiled_libc", "empty_libc", "wrong_crt")
    for mutation in mutations:
        changed = json.loads(json.dumps(graph))
        flags = changed["actions"][0]["paramFiles"][0]["arguments"]
        if mutation == "dynamic":
            flags.remove("-static")
        elif mutation == "shared":
            flags.append("external/llvm/libcxx/libc++.so.1")
        elif mutation == "undeclared_archive":
            changed["depSetOfFiles"][0]["directArtifactIds"].remove(1)
        elif mutation == "missing_libc":
            changed["depSetOfFiles"][1]["directArtifactIds"] = [7]
        elif mutation == "uncompiled_libc":
            changed["actions"][4]["mnemonic"] = "Symlink"
        elif mutation == "empty_libc":
            changed["depSetOfFiles"][3]["directArtifactIds"] = []
        elif mutation == "wrong_crt":
            changed["actions"][6]["mnemonic"] = "Symlink"
        else:
            changed["actions"].pop(1)
        try:
            static_links(changed)
        except ValueError:
            continue
        raise ValueError("Static-link graph predicate accepts " + mutation)
    return {"status": "PASS", "controls": 1 + len(mutations)}


def link_graph(bazel, common, work):
    """Analyze, but never execute, the two normal C++ inputs of the syscall entry point."""
    selected = "set(" + " ".join(LINK_TARGETS) + ")"
    query = ('outputs(".*/(fork_test|setup_container|glibc_library_search_directory|crt_objects_directory_linux|libc[.]a|libm[.]a|r?crt1[.]o|crti[.]o|crtn[.]o)$", '
             'deps(' + selected + '))')
    raw = work / "link-graph.json"
    started = now()
    with raw.open("w") as output, (work / "link-graph.log").open("w") as log:
        code = subprocess.run(["timeout", "--signal=INT", "--kill-after=25s", "12m",
                               *bazel, "aquery", *common, "--output=jsonproto",
                               "--include_artifacts", "--include_commandline", "--include_param_files", query],
                              stdout=output, stderr=log).returncode
    result = {"status": "INCOMPLETE", "native_exit": code, "started_at": started,
              "execution_finished_at": now()}
    log_text = (work / "link-graph.log").read_text(errors="replace")
    result["invocation_ids"] = sorted(set(re.findall(r"https://app\.buildbuddy\.io/invocation/([0-9a-f-]{36})", log_text)))
    if code:
        result["diagnostic_lines"] = [line[:1200] for line in log_text.splitlines()
                                      if re.search(r"^ERROR:|Error in |Traceback|no such file", line)
                                      and not re.search(r"api.key|remote.header|bes.header|authorization|credential|token=", line, re.I)][:12]
    if code == 0:
        try:
            result.update(static_links(json.loads(raw.read_text())))
        except (KeyError, ValueError) as err:
            result["reason"] = str(err)
    result["verified_at"] = now()
    return result


def qualify(arch, variant, source, tmp, out, private, bazel):
    key = variant + "-" + arch
    work = private / key
    work.mkdir()
    public = out / key
    public.mkdir()
    row = {"source": source, "variant": variant, "arch": arch, "targets": TARGETS, "required_cases": CASES, "libc": ABI, "phases": {}, "status": "INCOMPLETE"}
    write(public / "receipt.json", row)
    (work / "commit").write_bytes(git("cat-file", "commit", source))
    (work / "before").write_bytes(git("diff", "HEAD", "--"))
    (work / "expected.txt").write_text("\n".join(TARGETS) + "\n")
    write(work / "cases.json", CASES)
    platform = "//test/rbe/empty_worker:" + arch + "_musl"
    target_platform = "@llvm//platforms:linux_" + {"amd64": "x86_64", "arm64": "aarch64"}[arch] + "_glibc_static"
    row["target_platform"] = target_platform
    row["execution_platform"] = platform
    common = ["--config=" + {"amd64": "x86_64", "arm64": "aarch64"}[arch],
              "--platforms=" + target_platform, "--dynamic_mode=off",
              "--compilation_mode=fastbuild", "--host_platform=" + platform,
              "--extra_execution_platforms=" + platform,
              "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
              "--extra_toolchains=//test/rbe/go_bootstrap:linux_" + arch + "_ash",
              "--noincompatible_exclude_starlark_flags_from_exec_config",
              "--workspace_status_command=", "--host_features=fully_static_link",
              "--incompatible_use_native_posix_test_wrapper", "--nocache_test_results",
              "--nobuild_event_json_file_path_conversion", "--jobs=400",
              "--remote_download_outputs=toplevel",
              "--remote_download_regex=bazel-out/[^/]+/bin/(test/syscalls/(fork_test_native|linux/fork_test)|test/runner/setup_container/setup_container)$",
              "--remote_accept_cached", "--test_timeout=90", "--test_env=GTEST_FILTER=ForkTest.Simple:ForkTest.Pipe:ForkTest.SharedMapping",
              "--noremote_upload_local_results",
              "--noremote_local_fallback", "--spawn_strategy=remote", "--keep_going",
              "--color=no", "--curses=no"]
    row["phases"]["static_link_graph"] = link_graph(bazel, common, work)
    write(public / "receipt.json", row)
    if row["phases"]["static_link_graph"]["status"] != "PASS":
        return row
    extra = []
    for phase in ("analysis", "test"):
        raw = work / (phase + ".bep")
        flags = ["--nobuild"] if phase == "analysis" else ["--execution_log_compact_file=" + str(work / "actions.compact")]
        args = ["test", *common, *extra, *flags, "--build_event_json_file=" + str(raw), *TARGETS]
        started = now()
        with (work / (phase + ".log")).open("w") as log:
            code = subprocess.run(["timeout", "--signal=INT", "--kill-after=25s", "12m" if phase == "analysis" else "43m",
                                   *bazel, *args], stdout=log, stderr=subprocess.STDOUT).returncode
        finished = now()
        phase_log = (work / (phase + ".log")).read_text(errors="replace")
        invocation_ids = sorted(set(re.findall(r"https://app\.buildbuddy\.io/invocation/([0-9a-f-]{36})", phase_log)))
        diagnostics = [line[:1000] for line in phase_log.splitlines()
                       if re.search(r"error:|no such file|not found|permission denied|exec format|cannot execute|bad substitution|syntax error|go: |fork/exec", line, re.I)
                       and not re.search(r"api.key|remote.header|bes.header|authorization", line, re.I)][:12]
        row["phases"][phase] = {"started_at": started, "execution_finished_at": finished, "native_exit": code,
                                "invocation_ids": invocation_ids, "diagnostic_lines": diagnostics if code else []}
        write(public / "receipt.json", row)
        if phase == "analysis":
            preflight = report.preflight(raw, code, "test", work / "cases.json")
            write(public / "analysis.json", preflight)
            if preflight["status"] != "PASS":
                return row
            extra = preflight["output_options"]
            binaries = next(arg.split("=", 1)[1] for arg in common if arg.startswith("--remote_download_regex="))
            extra = ["--remote_download_regex=(" + binaries + ")|(" + arg.split("=", 1)[1] + ")"
                     if arg.startswith("--remote_download_regex=") else arg for arg in extra]
            continue
        (work / "arguments").write_text("\n".join(args) + "\n")
        (work / "native_exit").write_text(str(code))
        (work / "after").write_bytes(git("diff", "HEAD", "--"))
        (work / "head").write_bytes(git("rev-parse", "HEAD"))
        execroot = Path(subprocess.check_output([*bazel, "info", "execution_root"], stderr=subprocess.DEVNULL, text=True).strip())
        sanitized = work / "sanitized.bep"
        capture = report.capture(raw, sanitized, execroot, work / "reports", False, (work / "analysis.bep",), "test")
        (work / "capture_exit").write_text(str(int(bool(capture["errors"]))))
        context = {"expected_source": source, "commit_file": "commit", "source_before": "before",
                   "source_after": "after", "final_head": "head", "executed_arguments": "arguments",
                   "native_exit": "native_exit", "capture_exit": "capture_exit",
                   "execution_finished_at": finished, "command_kind": "test"}
        write(work / "context.json", context)
        verdict = report.verdict(sanitized, report.manifest_path(sanitized), work / "context.json",
                                 work / "expected.txt", (work / "analysis.bep",), case_manifest=work / "cases.json")
        write(public / "tests.json", verdict)
        for xml in (work / "reports").rglob("test.xml"):
            relative = xml.relative_to(work / "reports")
            destination = public / "xml" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(xml, destination)
        row["original_log_diagnostics"] = failure_lines(work / "reports")
        row["tests_status"] = verdict["status"]
        row["status"] = verdict["status"]
        # Preserve the test verdict before any independent worker-log decoding.
        write(public / "receipt.json", row)
        row["actions"] = native_actions(work / "actions.compact", arch, tmp)
        # Runtime status is retained even if output inspection later fails.
        write(public / "receipt.json", row)
        row["original_files"] = product_files(bazel, common, work, execroot, arch)
        row["verified_at"] = now()
        states = [verdict["status"], row["actions"]["status"], row["original_files"]["status"]]
        row["status"] = "FAIL" if "FAIL" in states else "PASS" if states == ["PASS"] * 3 else "INCOMPLETE"
        write(public / "receipt.json", row)
    return row


def main():
    tmp = Path(os.environ["RUNNER_TEMP"])
    out = tmp / "native-package-evidence"
    out.mkdir(exist_ok=True)
    private = tmp / "native-package-private"
    private.mkdir(exist_ok=True)
    write(out / "static-link-controls.json", static_link_controls())
    control = json.loads((tmp / "native-wrapper-control.json").read_text())
    actual = control["test_action"]
    targets = [actual["target"]]
    if not original_tests([actual], targets):
        raise ValueError("The verifier rejects the retained successful native TestRunner")
    if original_tests([dict(actual, executable="external/bazel_tools/tools/test/test-setup.sh")], targets):
        raise ValueError("The verifier accepts a shell TestRunner as native")
    write(out / "wrapper-control.json", {"status": "PASS", "provenance": control["provenance"]})
    try:
        subprocess.run(["git", "checkout", "--detach", SOURCE], check=True, stdout=subprocess.DEVNULL)
        if git("rev-parse", "HEAD").decode().strip() != SOURCE or git("diff", "HEAD", "--").strip():
            raise ValueError("Product source does not match the qualified commit")
        binary = tmp / "bazel-candidate"
        binary_sha256 = hashlib.sha256(binary.read_bytes()).hexdigest()
    except (subprocess.CalledProcessError, OSError, ValueError) as err:
        write(out / "summary.json", {"source": SOURCE, "status": "INCOMPLETE",
                                    "phase": "source_and_supplier_inputs", "executed_targets": 0,
                                    "collection_error": str(err), "verified_at": now()})
        return 1
    bazel = [str(binary), "--bazelrc=" + str(tmp / "sidecar.bazelrc")]
    rows = []
    for arch in ("amd64", "arm64"):
        variant, source = "native_launcher", SOURCE
        key = variant + "-" + arch
        try:
            subprocess.run(["git", "checkout", "--detach", source], check=True, stdout=subprocess.DEVNULL)
            if git("rev-parse", "HEAD").decode().strip() != source or git("diff", "HEAD", "--").strip():
                raise ValueError("The control source was not selected cleanly")
            rows.append(qualify(arch, variant, source, tmp, out, private, bazel))
        except Exception as err:
            # Retain any already-written original result, including test failure.
            path = out / key / "receipt.json"
            row = json.loads(path.read_text()) if path.exists() else {"arch": arch, "status": "INCOMPLETE"}
            if row.get("phases", {}).get("test", {}).get("native_exit", 0):
                row["status"] = "FAIL"
            elif row.get("status") != "FAIL":
                row["status"] = "INCOMPLETE"
            row["collection_error"] = type(err).__name__ + ": " + str(err)[:1400]
            row["verified_at"] = now()
            write(path, row)
            rows.append(row)
    status = "FAIL" if any(row["status"] == "FAIL" for row in rows) else "PASS" if all(row["status"] == "PASS" for row in rows) else "INCOMPLETE"
    summary = {"source": SOURCE, "status": status, "rows": rows, "libc": ABI, "verified_at": now(),
               "native_bazel": {"supplier_source": "c2f5f79dce24877679cede14b85666b7ca65c6de",
                                "member": "bazel-candidate",
                                "sha256": binary_sha256,
                                "identity": "Canonical supplier build; static embedded ELF verified in supplier.json",
                                "wrapper_prerequisite": "https://github.com/bazelbuild/bazel/pull/30837",
                                "prior_native_xml_proof": "https://github.com/tamird/gvisor/actions/runs/41071795282/artifacts/12986015991"}}
    write(out / "summary.json", summary)
    return int(summary["status"] != "PASS")


if __name__ == "__main__":
    sys.exit(main())
