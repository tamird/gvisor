"""Verify the exact selected TestRunners and the normal LLVM and Nogo workers."""

from collections import Counter, defaultdict
import json
import re
import subprocess

from native_graph import fields, output_ids, project_dump, test_log, varint


IMAGE = "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"
WORKER = re.compile(r"@{1,2}[^/]*syscall_size_worker//:amd64")
PRODUCERS = {"CppCompile", "CppLink", "GoStandardLibraryAnalysis", "GoStaticAnalysis"}


def actions(path):
    with path.open("rb") as stream:
        while prefix := stream.read(1):
            while prefix[-1] & 128:
                part = stream.read(1)
                assert part and len(prefix) < 10
                prefix += part
            size, _ = varint(prefix, 0)
            raw = stream.read(size)
            assert len(raw) == size
            for action in fields(raw)[2]:
                yield fields(action)


def messages(path):
    lines = []
    for index, text in enumerate(path.open(errors="replace"), 1):
        if not text.startswith(("ERROR:", "FATAL:")):
            continue
        line = re.sub(r"(?:https?|grpcs?)://\S+|\b[\w+/=-]{48,}\b", "<omitted>", text.strip())
        if not re.search(r"authorization|api[-_]key|header=|bearer |token=|password=", line, re.I):
            lines.append([index, "".join(c for c in line if c.isprintable())[:450]])
        if len(lines) == 8:
            break
    return lines


def preflight(work, bazel, common, owners, checkpoint):
    # The only parent constraint platform with external visibility is the
    # public native AMD64 platform. Supply the same OCI worker properties as
    # //test/rbe:ubuntu_amd64, with two overrides for the test exec group.
    worker = work / "worker"
    worker.mkdir()
    (worker / "MODULE.bazel").write_text('module(name = "syscall_size_worker")\n')
    (worker / "BUILD.bazel").write_text('''platform(
    name = "amd64",
    parents = ["@@//tools/bazeldefs:linux_amd64"],
    exec_properties = {
        "Arch": "amd64",
        "OSFamily": "linux",
        "container-image": "%s",
        "workload-isolation-type": "oci",
        "test.workload-isolation-type": "firecracker",
        "test.dockerUser": "root",
    },
    visibility = ["//visibility:public"],
)
''' % IMAGE)
    local = ["--spawn_strategy=local", "--strategy=TestRunner=local", "--remote_executor=", "--remote_cache=", "--bes_backend="]
    bep, dump, log = (work / n for n in ("graph.bep", "graph.pb", "graph.log"))
    with log.open("wb") as out:
        code = subprocess.run([*bazel, "test", *common, *local, "--nobuild",
                               "--build_event_json_file=" + str(bep),
                               "--experimental_aquery_dump_after_build_format=streamed_proto",
                               "--experimental_aquery_dump_after_build_output_file=" + str(dump), *owners],
                              stdout=out, stderr=out, timeout=900).returncode
    report = {"qualified": False, "analysis_exit": code, "diagnostics": messages(log)}
    selected, sizes, trials, finish = {}, {}, 0, []
    if bep.exists():
        for line in bep.open():
            e = json.loads(line)
            ident = e["id"]
            if "action" in e or "testResult" in e:
                trials += 1
            if cfg := ident.get("targetConfigured"):
                if size := e.get("configured", {}).get("testSize"):
                    sizes[cfg["label"]] = size.lower()
            if done := ident.get("targetCompleted"):
                if not done.get("aspect") and e.get("aborted", {}).get("reason") == "NO_BUILD":
                    selected[done["label"]] = done["configuration"]["id"]
            if "finished" in e:
                finish.append(e["finished"].get("exitCode", {}))
    report.update(sizes=sizes, selection=selected, finish=finish, executed=trials)
    checkpoint(report)
    if trials or sizes != owners or selected.keys() != owners.keys() or not dump.is_file():
        return report
    graph = project_dump(dump)
    runners, keys, examples, violations = {}, {}, {}, []
    counts = defaultdict(Counter)
    for row in actions(dump):
        kind = row[4][0].decode()
        owner = graph["targets"][str(row[1][0])]
        platform = (row[14] or [b""])[0].decode()
        props = {r[1][0].decode(): (r[2] or [b""])[0].decode() for r in (fields(x) for x in row[11])}
        cls = props.get("workload-isolation-type", "")
        if kind == "TestRunner":
            keys[(owner, graph["configs"][str(row[5][0])], tuple(output_ids(row[9])))] = (row[3] or [b""])[0].decode()
        if kind in PRODUCERS:
            ok = bool(WORKER.fullmatch(platform) and cls == "oci" and props.get("Arch") == "amd64"
                      and props.get("OSFamily") == "linux" and props.get("container-image") == IMAGE
                      and not props.get("dockerUser"))
            counts[kind]["native_oci" if ok else "mismatch"] += 1
            if kind not in examples:
                examples[kind] = {"owner": owner, "platform": platform, "isolation": cls,
                                  "action_key": (row[3] or [b""])[0].decode()}
        if kind != "TestRunner" and (cls == "firecracker" or props.get("dockerUser") == "root") and len(violations) < 12:
            violations.append({"mnemonic": kind, "owner": owner, "platform": platform})
    for action in graph["actions"]:
        owner = graph["targets"][str(action["targetId"])]
        config = graph["configs"][str(action["configurationId"])]
        if selected.get(owner) != config or not test_log(action, graph["artifacts"], graph["fragments"]):
            continue
        assert owner not in runners
        props = {p["key"]: p["value"] for p in action["executionInfo"]}
        platform = action["executionPlatform"]
        runners[owner] = {"configuration": config, "platform": platform, "size": sizes[owner],
                          "action_key": keys[owner, config, tuple(action["outputIds"])],
                          "worker": {k: props.get(k) for k in ("Arch", "OSFamily", "workload-isolation-type", "dockerUser")},
                          "matches": bool(WORKER.fullmatch(platform) and props.get("Arch") == "amd64"
                                          and props.get("OSFamily") == "linux" and props.get("container-image") == IMAGE
                                          and props.get("workload-isolation-type") == "firecracker"
                                          and props.get("dockerUser") == "root")}
    report.update(runners=runners, producer_counts=dict(counts), examples=examples, unexpected_privileged_actions=violations)
    checkpoint(report)
    configs = {}
    for checksum in sorted(set(selected.values())):
        raw = subprocess.check_output([*bazel, "config", "--output=json", checksum])
        cfg = json.loads(raw)
        opts = {k: v for fragment in cfg["fragmentOptions"] for k, v in fragment["options"].items()
                if k in ("cpu", "platforms")}
        configs[checksum] = {"target": not cfg["isExec"] and cfg["configHash"] == checksum, **opts}
    report["target_configurations"] = configs
    checkpoint(report)
    fixtures = ["//test/syscalls/linux:processes_test", "//test/syscalls/linux:memory_accounting_test"]
    query = 'mnemonic("^(CppCompile|CppLink)$", set(' + " ".join(fixtures) + "))"
    compiler_graph = work / "compiler.json"
    with compiler_graph.open("wb") as out, (work / "compiler.log").open("wb") as err:
        query_flags = [flag for flag in common if flag != "--build_tests_only"]
        compiler_rc = subprocess.run([*bazel, "aquery", *query_flags, *local, "--output=jsonproto",
                                      "--noinclude_artifacts", "--include_commandline", query],
                                     stdout=out, stderr=err, timeout=300).returncode
    compiler = json.loads(compiler_graph.read_text()) if compiler_rc == 0 else {}
    labels = {int(target["id"]): target["label"].lstrip("@") for target in compiler.get("targets", [])}
    tools = []
    for act in compiler.get("actions", []):
        args = act.get("arguments", [])
        exe = args[0] if args else ""
        tools.append({"label": labels[int(act["targetId"])], "mnemonic": act["mnemonic"],
                      "executable": exe if re.fullmatch(r"[\w./@+~-]{1,350}", exe) else None,
                      "llvm": bool(re.search(r"(?:^|/)external/llvm[^/]*/", exe))})
    report.update(compiler_actions=tools, compiler_diagnostics=messages(work / "compiler.log"))
    compiler_ok = (compiler_rc == 0 and {(t["label"], t["mnemonic"]) for t in tools}
                   == {(label, mnemonic) for label in fixtures for mnemonic in ("CppCompile", "CppLink")}
                   and all(t["llvm"] for t in tools))
    report["qualified"] = (code == 1 and finish == [{"code": 1, "name": "BUILD_FAILURE"}] and not violations
                           and runners.keys() == owners.keys() and all(r["matches"] for r in runners.values())
                           and all(c["target"] and c["cpu"] == "k8" and c["platforms"].replace("@", "") == "[//tools/bazeldefs:linux_amd64]"
                                   for c in configs.values())
                           and compiler_ok and counts.keys() == PRODUCERS
                           and all(set(c) == {"native_oci"} for c in counts.values()))
    return report
