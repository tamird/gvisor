"""Check the native Nogo worker and retain actual cross-compiler placement."""

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

from stdlib_provenance import digest, fields, first, number

ROOT = Path(os.environ["RUNNER_TEMP"])
OUT = ROOT / "nogo-receipts"
IMAGE = "docker://mirror.gcr.io/library/ubuntu@sha256:78d4744ead1f5b19b04d0df558ab182a5300903900755e515b2ca4d10eb2044d"
PROPS = {"container-image": IMAGE, "Arch": "amd64", "OSFamily": "linux",
         "workload-isolation-type": "oci", "recycle-runner": "false"}
COWNERS = {"//vdso:vdso"} | {"//pkg/sentry/platform/systrap/sysmsg:" + name
           for name in ("sighandler", "sysmsg_lib", "sysrestorer", "syshandler")}
TOOLS = re.compile(r"(?:external/|bazel-out/)[^\s\"';]+/(?:clang(?:\+\+)?|ld\.lld|llvm-(?:nm|objcopy))(?=[\s\"';]|$)")


def write(name, value):
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    require(len(data) < 4 * 1024 * 1024, "bounded placement report exceeded 4MiB")
    (OUT / (name + ".json")).write_bytes(data)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def integers(values):
    result = []
    for value in values:
        if isinstance(value, int):
            result.append(value)
        else:
            stream = BytesIO(value)
            while (item := number(stream)) is not None:
                result.append(item)
    return result


def command_files(args, declared):
    return [{"index": index, "path": arg.removeprefix("@"),
             "response_file": arg.startswith("@"),
             "digest": declared.get(arg.removeprefix("@"))}
            for index, arg in enumerate(args)
            if re.fullmatch(r"@?(?:external|bazel-out)/[a-zA-Z0-9_+./-]+", arg)]


def inspect(path, commands=False):
    files, inputs, selected, symlinks = {}, {}, [], []
    counts = Counter()
    invocation = None
    with subprocess.Popen(["zstd", "-qdc", str(path)], stdout=subprocess.PIPE) as process:
        while (size := number(process.stdout)) is not None:
            entry = fields(process.stdout.read(size))
            key = first(entry, 1, 0)
            if 2 in entry:
                invocation = first(fields(first(entry, 2)), 4).decode()
            if 3 in entry:
                item = fields(first(entry, 3))
                files[key] = {"path": first(item, 1).decode(), "digest": digest(first(item, 2))}
            if 6 in entry:
                item = fields(first(entry, 6))
                inputs[key] = (integers(item.get(5, [])), integers(item.get(4, [])))
            if commands and 8 in entry:
                item = fields(first(entry, 8))
                link = {name: first(item, field).decode() for name, field in
                        (("input", 1), ("output", 2), ("owner", 3), ("mnemonic", 4))}
                if "link-wrapper-clang++" in link["output"]:
                    symlinks.append(link)
            if 7 not in entry:
                continue
            spawn = fields(first(entry, 7))
            owner = first(spawn, 7).decode().removeprefix("@@").removeprefix("@")
            mnemonic = first(spawn, 8).decode()
            runner = first(spawn, 11).decode()
            counts[(mnemonic, runner, bool(first(spawn, 12, 0)))] += 1
            args = [v.decode() for v in spawn.get(1, [])]
            if owner not in COWNERS and not owner.endswith("//:no_compiler") and mnemonic not in {"CppCompile", "CppLink"} and owner != "//runsc/cgroup:cgroup_test":
                continue
            props = {first(fields(v), 1).decode(): first(fields(v), 2).decode()
                     for v in fields(first(spawn, 3)).get(1, [])}
            ids, pending = set(), [first(spawn, 4, 0)]
            visited = set()
            while pending:
                item = pending.pop()
                if not item or item in visited:
                    continue
                visited.add(item)
                direct, transitive = inputs[item]
                ids.update(direct)
                pending.extend(transitive)
            declared = {files[i]["path"]: files[i]["digest"] for i in ids if i in files}
            tools = sorted(set(TOOLS.findall(" ".join(args))))
            outputs = [files[i] for v in spawn.get(6, [])
                       if (i := first(fields(v), 5, 0)) in files]
            row = {"owner": owner, "mnemonic": mnemonic, "runner": runner,
                   "cached": bool(first(spawn, 12, 0)), "exit": first(spawn, 9, 0),
                   "status": first(spawn, 10).decode(),
                   "digest": digest(first(spawn, 16)), "platform": {k: props.get(k) for k in PROPS},
                   "argv_sha256": hashlib.sha256(json.dumps(args).encode()).hexdigest(),
                   "tools": [{"path": tool, "digest": declared.get(tool)} for tool in tools],
                   "target_triples": sorted(set(re.findall(r"(?:x86_64|aarch64)[-a-z0-9]*linux[-a-z0-9]*", " ".join(args)))),
                   "outputs": outputs}
            if commands and mnemonic in {"CppCompile", "CppLink"} and not tools:
                # Only file identities from the two unmatched original commands;
                # never their environment or arbitrary command-line strings.
                row["command_files"] = command_files(args, declared)
                row["argument_count"] = len(args)
                env = {first(fields(item), 1).decode(): first(fields(item), 2).decode()
                       for item in spawn.get(2, [])
                       if first(fields(item), 1) in {b"LLVM_CLANGXX", b"LLVM_DSYM_PATH", b"LLVM_GENERATE_INTERFACE_LIBRARY"}}
                clangxx = env.get("LLVM_CLANGXX", "")
                row["linker_environment"] = {
                    "clangxx": {"path": clangxx, "digest": declared.get(clangxx)} if TOOLS.fullmatch(clangxx) else None,
                    "dsym_requested": bool(env.get("LLVM_DSYM_PATH")),
                    "interface_requested": env.get("LLVM_GENERATE_INTERFACE_LIBRARY") == "yes",
                }
                row["declared_compiler_tools"] = [
                    {"path": path, "digest": key} for path, key in sorted(declared.items())
                    if TOOLS.fullmatch(path)]
            if (commands and mnemonic in {"CppCompile", "CppLink"}
                    and any("link-wrapper-clang++" in output["path"] for output in outputs)):
                related = [{"path": path, "digest": key} for path, key in declared.items()
                           if "link_wrapper" in path.rsplit("/", 1)[-1]]
                related.sort(key=lambda item: item["path"])
                if related:
                    row["wrapper_inputs"] = related
                    row["wrapper_command_files"] = command_files(args, declared)
                    row["wrapper_compile_sources"] = [item for item in row["wrapper_command_files"]
                                                      if item["index"] and args[item["index"] - 1] == "-c"]
            selected.append(row)
        require(process.wait() == 0, "incomplete compact log")
    return {"invocation": invocation, "actions": selected, "symlinks": symlinks,
            "counts": [{"mnemonic": k[0], "runner": k[1], "cached": k[2], "count": v}
                       for k, v in sorted(counts.items())]}



def valid_digest(value):
    return (isinstance(value, list) and len(value) == 2
            and isinstance(value[0], str) and re.fullmatch(r"[a-f0-9]{64}", value[0])
            and isinstance(value[1], int) and value[1] > 0)


def summarize_compilers(value):
    cpp = [a for a in value["actions"] if a["mnemonic"] in {"CppCompile", "CppLink"}]
    groups = defaultdict(list)
    for action in cpp:
        groups[action["owner"], action["mnemonic"]].append(action)
    issues = [a for a in cpp if a["runner"] != "remote" or a["platform"] != PROPS
              or a["exit"] != 0 or a["status"] or a["cached"]
              or not a["tools"] or not all(valid_digest(t["digest"]) for t in a["tools"])]
    return {"invocation": value["invocation"], "counts": value["counts"],
            "compilers": [a for a in value["actions"] if a["owner"] in COWNERS],
            "local_go_actions": [a for a in value["actions"] if a["owner"] == "//runsc/cgroup:cgroup_test"
                                 and a["mnemonic"] in {"GoCompilePkg", "GoStaticAnalysis"}],
            "cpp_summary": {"actions": len(cpp), "mnemonics": dict(Counter(a["mnemonic"] for a in cpp)),
                            "rows_sha256": hashlib.sha256(json.dumps(cpp, sort_keys=True).encode()).hexdigest()},
            "cpp_owners": [{"owner": owner, "mnemonic": mnemonic, "actions": len(actions),
                            "rows_sha256": hashlib.sha256(json.dumps(actions, sort_keys=True).encode()).hexdigest()}
                           for (owner, mnemonic), actions in sorted(groups.items())],
            "issue_groups": [{"owner": owner, "mnemonic": mnemonic, "actions": count}
                             for (owner, mnemonic), count in sorted(Counter((a["owner"], a["mnemonic"]) for a in issues).items())],
            "initial_issue_count": len(issues), "issue_examples": issues[:8]}


def resolve_output(path: str, symlinks: list[dict[str, str]]) -> tuple[str, list[dict[str, str]]]:
    """Follow Bazel's executed SymlinkAction records, not filesystem guesses."""
    links = defaultdict(list)
    for link in symlinks:
        links[link["output"]].append(link)
    chain = []
    visited = set()
    while path in links:
        require(path not in visited, "cyclic wrapper symlink")
        require(len(links[path]) == 1, "ambiguous wrapper symlink")
        require(len(chain) < 16, "wrapper symlink chain exceeds bound")
        visited.add(path)
        link, = links[path]
        chain.append(link)
        path = link["input"]
    return path, chain


def resolve_links(actions, symlinks, report, read_file, save):
    cpp = [a for a in actions if a["mnemonic"] in {"CppCompile", "CppLink"}]
    report["indirect_links"] = [a for a in cpp if not a["tools"]]
    for action in report["indirect_links"]:
        action["indirection_verified"] = False
        save()
        command = action.get("command_files", [])
        if (action["mnemonic"] != "CppLink" or len(action["outputs"]) != 1
                or not valid_digest(action["outputs"][0]["digest"])
                or action.get("argument_count") != 2
                or [item["index"] for item in command] != [0, 1]):
            continue
        driver, params = command
        env = action.get("linker_environment", {})
        clangxx = env.get("clangxx")
        if (driver["response_file"] or not params["response_file"]
                or not driver["path"].endswith("/link-wrapper-clang++")
                or not valid_digest(driver["digest"]) or not valid_digest(params["digest"])
                or not clangxx or not clangxx["path"].endswith("/clang++") or not valid_digest(clangxx["digest"])
                or env["dsym_requested"] or env["interface_requested"]):
            continue
        produced_path, chain = resolve_output(driver["path"], symlinks)
        action["wrapper_symlink_chain"] = chain
        # v0.8.24 link_wrapper.c calls execv(LLVM_CLANGXX, argv). Bind the
        # executed wrapper through its actual object producer to that source.
        objects = [a for a in cpp if a["mnemonic"] == "CppCompile" and any(
            item["path"].endswith("/tools/internal/link_wrapper.c") and item["digest"] ==
            ["8b29485c9804c8b8a986659949ae7436f255d78f49d1abc852c93502660e4a63", 9273]
            for item in a.get("wrapper_compile_sources", []))]
        producers = [a for a in cpp if a["mnemonic"] == "CppLink"
                     and {"path": produced_path, "digest": driver["digest"]} in a["outputs"]
                     and any(item in obj["outputs"] for item in a.get("wrapper_inputs", []) for obj in objects)]
        action["wrapper_compile_producers"] = objects
        action["wrapper_link_producers"] = producers
        if len(producers) != 1 or not objects:
            continue
        producer, = producers
        link_files = producer.get("wrapper_command_files", [])
        link_args = [item["path"] for item in link_files if not item["response_file"]]
        for item in link_files:
            if item["response_file"]:
                link_args.extend(shlex.split(read_file(item).decode()))
        producer["bound_wrapper_objects"] = [item for item in producer["wrapper_inputs"]
                                              if item["path"] in link_args
                                              and any(item in obj["outputs"] for obj in objects)]
        if not producer["bound_wrapper_objects"]:
            continue
        raw = read_file(params)
        args = shlex.split(raw.decode())
        params["compiler_paths"] = sorted(set(TOOLS.findall(raw.decode())))
        output_paths = [args[i + 1] for i in range(len(args) - 1) if args[i] == "-o"]
        if output_paths != [item["path"] for item in action["outputs"]]:
            continue
        selected = {tool["path"]: tool for tool in action["declared_compiler_tools"]}
        if not all(path in selected and valid_digest(selected[path]["digest"]) for path in params["compiler_paths"]):
            continue
        action.update(tools=[clangxx] + [selected[path] for path in params["compiler_paths"] if path != clangxx["path"]],
                      indirection_verified=True, classification="shared_library_link")
        save()


def validate_cpp(cpp, report):
    tools = Counter((tool["path"], *tool["digest"]) for action in cpp for tool in action["tools"]
                    if valid_digest(tool["digest"]))
    report["tool_inputs"] = [{"path": path, "digest": [sha, size], "actions": count}
                             for (path, sha, size), count in sorted(tools.items())]
    report["cpp_summary"].update(
        wrong_placement=sum(a["runner"] != "remote" or a["platform"] != PROPS for a in cpp),
        unresolved_tools=sum(not a["tools"] or not all(valid_digest(t["digest"]) for t in a["tools"]) for a in cpp),
        non_success_results=sum(a["exit"] != 0 or a["status"] != "" for a in cpp),
        cache_hits=sum(a["cached"] for a in cpp),
        verified_indirect_links=sum(a.get("indirection_verified", False) for a in cpp))
    report["remaining_issue_count"] = sum(
        a["runner"] != "remote" or a["platform"] != PROPS or a["exit"] != 0 or a["status"] != "" or a["cached"]
        or not a["tools"] or not all(valid_digest(t["digest"]) for t in a["tools"]) for a in cpp)
    require(cpp and report["remaining_issue_count"] == 0,
            "C++ action placement or input evidence incomplete")

def check(mode, value=None, read_file=None, expected_invocation=None):
    path = ROOT / ("probe.compact" if mode == "probe" else "nogo.compact")
    if value is None:
        value = inspect(path, commands=mode != "probe")
    report = value if mode == "probe" else summarize_compilers(value)
    raw = path.read_bytes()
    report.update(verified=False, source=os.environ["PRODUCT"],
                  compact_log={"hash": hashlib.sha256(raw).hexdigest(), "sizeBytes": len(raw)})
    write(mode, report)

    def load(item):
        require(valid_digest(item["digest"]), "missing file digest")
        if read_file is not None:
            data = read_file(item)
        elif Path(item["path"]).is_file():
            data = Path(item["path"]).read_bytes()
        else:
            from cas_inputs import blob
            data = blob({"hash": item["digest"][0], "sizeBytes": item["digest"][1]}, value["invocation"])
        require([hashlib.sha256(data).hexdigest(), len(data)] == item["digest"], "file bytes differ")
        return data

    try:
        rows = value["actions"]
        if mode == "probe":
            require(len(rows) == 1, "missing unique worker probe")
            action, = rows
            require(action["owner"].endswith("//:no_compiler") and action["exit"] == 0 and not action["status"]
                    and not action["cached"] and action["runner"] == "remote" and action["platform"] == PROPS,
                    "worker probe did not execute on the pinned image")
            output, = action["outputs"]
            require(load(output) == b"no host compiler; bash and awk available\n", "wrong probe output")
        else:
            if expected_invocation is None:
                expected_invocation = json.loads((OUT / "verdict.json").read_text())["invocation"]
            require(value["invocation"] == expected_invocation, "execution log invocation differs")
            compiled = report["compilers"]
            cpp = [a for a in rows if a["mnemonic"] in {"CppCompile", "CppLink"}]
            require(cpp, "no C++ action evidence")
            require(Counter(a["owner"] for a in compiled) == {owner: 2 for owner in COWNERS},
                    "missing cross-architecture compiler actions")
            require(all(valid_digest(a["digest"]) for a in compiled)
                    and len({tuple(a["digest"]) for a in compiled}) == len(compiled),
                    "missing or duplicate embedded action digest")
            for action in compiled:
                require(action["runner"] == "remote" and not action["cached"] and action["exit"] == 0
                        and not action["status"] and action["platform"] == PROPS, "embedded compiler execution differs")
                require(action["tools"] and all(valid_digest(tool["digest"]) for tool in action["tools"]),
                        "embedded compiler input digest missing")
                require(any("/clang" in tool["path"] for tool in action["tools"]) and action["target_triples"],
                        "missing declared embedded compiler or target triple")
                require(action["outputs"], "missing embedded compiler output")
                for output in action["outputs"]:
                    data = load(output)
                    require(data[:6] == b"\x7fELF\x02\x01", "wrong embedded ELF")
                    output["machine"] = int.from_bytes(data[18:20], "little")
                    require(output["machine"] in {62, 183}, "wrong embedded ELF architecture")
                write(mode, report)
            for owner in COWNERS:
                require({o["machine"] for a in compiled if a["owner"] == owner for o in a["outputs"]} == {62, 183},
                        "missing embedded target architecture")
            resolve_links(rows, value.get("symlinks", []), report, load, lambda: write(mode, report))
            validate_cpp(cpp, report)
            local = report["local_go_actions"]
            require(local and all(a["runner"] == "local" and a["exit"] == 0 and not a["status"] for a in local)
                    and {a["mnemonic"] for a in local} == {"GoCompilePkg", "GoStaticAnalysis"},
                    "explicit local Go placement differs")
        report.update(verified=True, verdict_available=datetime.now(timezone.utc).isoformat())
    finally:
        write(mode, report)
    return report


# Recovery reads only the completed invocation. The original worker experiment
# and its failed publication remain immutable on the parent carrier.
def recover():
    import importlib.util
    import urllib.parse
    from cas_inputs import blob, rpc

    original = "a71a698e9367395314448d19087e097087ee81ee"
    invocation = "82099240-e0c3-4136-b3ce-a0e504c6d2ee"
    report = {"source": os.environ["PRODUCT"], "original_carrier": original,
              "original_run": 41584824607, "original_job": 125747736901,
              "original_job_conclusion": "failure", "invocation": invocation,
              "workload_repeated": False, "phases": {}}

    def save():
        write("recovery", report)

    def phase(name, function):
        row = {"verified": False}
        report["phases"][name] = row
        save()
        try:
            function(row)
            row["verified"] = True
        except Exception as exc:
            row["verified"] = False
            row["error_type"] = type(exc).__name__
            if isinstance(exc, ValueError):
                row["error"] = str(exc)[:400]
        row["verdict_available"] = datetime.now(timezone.utc).isoformat()
        save()
        print(name, row["verified"], flush=True)

    def receipt(row):
        args = ["gh", "api"]
        if "--allow-escape-sequences" in subprocess.check_output([*args, "--help"], text=True):
            args.append("--allow-escape-sequences")
        raw = subprocess.check_output([*args, "repos/tamird/gvisor/actions/jobs/125747736901/logs"])
        require(hashlib.sha256(raw).hexdigest() ==
                "ccc91577781ca0a6ba3096de88e00b270b0d29b43bd001a4d7601c758cd94f54",
                "original Actions log changed")
        values = [json.loads(line.split(" ", 1)[1]) for line in raw.decode().splitlines()
                  if line.split(" ", 1)[-1].startswith('{"verified":')]
        require(len(values) == 1, "missing unique original verdict")
        value, = values
        require(value["source"] == os.environ["PRODUCT"] and value["invocation"] == invocation
                and value["verified"] and value["status"] == 0
                and value["bazel_exit"] == {"name": "SUCCESS"}
                and value["summaries"] == {"PASSED": 871}
                and value["configured"] == value["testRunners"] == 871
                and value["amd64Markers"] == value["arm64Markers"] == 871
                and not value["missing"] and not value["unexpected"] and not value["skipped"],
                "original workload did not pass the complete selection")
        summaries = [line for line in value["consoleTail"]
                     if re.fullmatch(r"Executed [0-9]+ out of [0-9]+ tests: [0-9]+ tests pass\.", line)]
        require(summaries == ["Executed 871 out of 871 tests: 871 tests pass."],
                "original Bazel console test count differs")
        row["bazel_test_summary"] = summaries[0]
        keys = ("verified", "source", "host", "status", "bazel_exit", "invocation",
                "configured", "testRunners", "summaries", "amd64Markers", "arm64Markers",
                "expected", "missing", "unexpected", "skipped", "agent_options",
                "execution_finished", "verdict_available", "started", "finished", "processes")
        row.update(log_sha256=hashlib.sha256(raw).hexdigest(), log_bytes=len(raw),
                   original_verdict={key: value[key] for key in keys},
                   original_projection_step="failure", original_upload_gate="filename/type/8MiB bound")

    records = {}

    def events(row):
        result = rpc("GetInvocation", {"lookup": {"invocationId": invocation}})
        inv, = result["invocation"]
        require(inv["invocationId"] == invocation, "different invocation returned")
        entries = [entry["buildEvent"] for entry in inv["event"]]
        # GetInvocation's metadata projection can omit individual test events.
        # Read attempts from the original compact stream, independently of it.
        row["api_test_result_events"] = sum("testResult" in entry for entry in entries)
        logs = [item for entry in entries for item in entry.get("buildToolLogs", {}).get("log", [])
                if item.get("name") == "execution_log.binpb.zst"]
        require(len(logs) == 1, "missing unique original compact log")
        uri = urllib.parse.urlsplit(logs[0]["uri"])
        match = re.fullmatch(r"(?:/[^?]*)?/blobs/([a-f0-9]{64})/(\d+)", uri.path)
        require(uri.scheme == "bytestream" and uri.netloc == "remote.buildbuddy.io"
                and not uri.query and match is not None, "unexpected original compact URI")
        key = {"hash": match[1], "sizeBytes": match[2]}
        row["compact_digest"] = key
        data = blob(key, invocation)
        (ROOT / "nogo.compact").write_bytes(data)
        records["compact"] = data
        selected, xml_generators = {}, {}
        with subprocess.Popen(["zstd", "-qdc", str(ROOT / "nogo.compact")], stdout=subprocess.PIPE) as process:
            while (size := number(process.stdout)) is not None:
                entry = fields(process.stdout.read(size))
                if 7 not in entry:
                    continue
                action = fields(first(entry, 7))
                if first(action, 8) != b"TestRunner":
                    continue
                owner = first(action, 7).decode().removeprefix("@@").removeprefix("@")
                args = [item.decode() for item in action.get(1, [])]
                scripts = [name for name in ("test-setup.sh", "generate-xml.sh")
                           if any(arg.endswith("/tools/test/" + name) for arg in args)]
                require(len(scripts) == 1, "unknown original TestRunner entry point")
                destination = selected if scripts[0] == "test-setup.sh" else xml_generators
                require(owner not in destination, "duplicate original TestRunner role")
                destination[owner] = {"runner": first(action, 11).decode(),
                                      "cached": bool(first(action, 12, 0)),
                                      "status": first(action, 10).decode(),
                                      "exit": first(action, 9, 0)}
            require(process.wait() == 0, "incomplete original compact test stream")
        row["attempts"] = selected
        row["xml_generators"] = xml_generators
        expected = set(report["phases"]["original"]["original_verdict"]["expected"])
        require(set(selected) == expected and len(selected) == 871, "original owner set differs")
        require(all(value == {"runner": "processwrapper-sandbox", "cached": False, "status": "", "exit": 0}
                    for value in selected.values()), "original TestRunner execution differs")


    def compiler(row):
        require("compact" in records, "original compact log unavailable")
        spec = importlib.util.spec_from_file_location("original_placement", ROOT / "original_placement.py")
        old = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(old)
        value = old.inspect(ROOT / "nogo.compact")
        require(value["invocation"] == invocation, "compact invocation differs")
        value.update(verified=False, source=os.environ["PRODUCT"])
        projected = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
        row.update(original_compiler_json_bytes=len(projected),
                   original_compiler_json_sha256=hashlib.sha256(projected).hexdigest(),
                   original_8MiB_bound_satisfied=len(projected) < 8 * 1024 * 1024,
                   counts=value["counts"])
        value = inspect(ROOT / "nogo.compact", commands=True)
        require(value["invocation"] == invocation, "command inspection invocation differs")
        import copy
        original_rows = copy.deepcopy(value)
        cache = {}

        def read(item):
            key = tuple(item["digest"])
            if key not in cache:
                cache[key] = blob({"hash": key[0], "sizeBytes": key[1]}, invocation)
            return cache[key]

        try:
            checked = check("compiler", value, read, invocation)
            links = checked["indirect_links"]
            require(len(links) == 2 and all(
                a["owner"] == "//tools/ioctl_sniffer:ioctl_hook" and a["indirection_verified"]
                and a["wrapper_symlink_chain"] for a in links), "original wrapper pair differs")
        finally:
            row.update(json.loads((OUT / "compiler.json").read_text()))
            save()
        records["compiler_rows"] = original_rows
        records["read_file"] = read
        records["compiler_report"] = checked

    def fixtures(row):
        import copy
        require("compiler_rows" in records, "positive compiler fixture unavailable")
        row["negative_cases"] = {}
        expected_errors = {
            "empty_cpp": "no C++ action evidence",
            "ambiguous_wrapper_symlink": "ambiguous wrapper symlink",
            "cyclic_wrapper_symlink": "cyclic wrapper symlink",
        }
        for case in ("empty_cpp", "missing_tool_digest", "unknown_wrapper_source", "wrong_placement",
                     "missing_wrapper_symlink", "ambiguous_wrapper_symlink", "cyclic_wrapper_symlink",
                     "wrong_wrapper_digest", "wrong_wrapper_object"):
            value = copy.deepcopy(records["compiler_rows"])
            if case == "empty_cpp":
                value["actions"] = [a for a in value["actions"] if a["mnemonic"] not in {"CppCompile", "CppLink"}]
            elif case == "missing_wrapper_symlink":
                value["symlinks"] = []
            elif case in {"ambiguous_wrapper_symlink", "cyclic_wrapper_symlink"}:
                first_link = records["compiler_report"]["indirect_links"][0]["wrapper_symlink_chain"][0]
                link, = [link for link in value["symlinks"] if link["output"] == first_link["output"]]
                if case == "ambiguous_wrapper_symlink":
                    value["symlinks"].append(copy.deepcopy(link))
                else:
                    link["input"] = link["output"]
            elif case == "wrong_wrapper_digest":
                for action in value["actions"]:
                    if action["mnemonic"] == "CppLink" and not action["tools"]:
                        action["command_files"][0]["digest"] = ["0" * 64, 1]
            elif case == "wrong_wrapper_object":
                for action in value["actions"]:
                    if action["mnemonic"] == "CppLink":
                        for item in action.get("wrapper_inputs", []):
                            item["digest"] = ["0" * 64, 1]
            elif case == "unknown_wrapper_source":
                for action in value["actions"]:
                    for item in action.get("wrapper_compile_sources", []):
                        if item["path"].endswith("/link_wrapper.c"):
                            item["digest"] = ["0" * 64, 9273]
            else:
                action = next(a for a in value["actions"]
                              if a["mnemonic"] == "CppCompile" and a["owner"] not in COWNERS)
                if case == "missing_tool_digest":
                    action["tools"][0]["digest"] = ["", 0]
                else:
                    action["runner"] = "local"
            try:
                check("compiler", value, records["read_file"], invocation)
            except ValueError as exc:
                result = json.loads((OUT / "compiler.json").read_text())
                require(not result["verified"], "negative fixture lost its failure")
                expected = expected_errors.get(case, "C++ action placement or input evidence incomplete")
                require(str(exc) == expected, "wrong negative fixture diagnostic: " + case)
                row["negative_cases"][case] = {"rejected": True, "error": str(exc)}
            else:
                raise ValueError("negative fixture was accepted: " + case)
        positive = records["compiler_report"]
        write("compiler", positive)
        raw = (OUT / "compiler.json").read_bytes()
        row.update(positive_summary_bytes=len(raw), positive_summary_sha256=hashlib.sha256(raw).hexdigest(),
                   cpp_owners=len(positive["cpp_owners"]), original_cpp_actions=positive["cpp_summary"]["actions"])
        require(len(raw) < 4 * 1024 * 1024 and positive["verified"], "unbounded or unsuccessful future summary")

    phase("original", receipt)
    phase("original_events", events)
    phase("compiler", compiler)
    phase("future_report_fixtures", fixtures)
    report["verdict_available"] = datetime.now(timezone.utc).isoformat()
    report["verified"] = all(p["verified"] for p in report["phases"].values())
    report["workload_verified"] = report["phases"]["original"]["verified"]
    save()
    raise SystemExit(0 if report["verified"] else 1)


def report_fixtures():
    """Exercise reporting contracts with metadata, never build or run targets."""
    import copy
    import zipfile

    raw = subprocess.check_output(["gh", "api", "repos/tamird/gvisor/actions/artifacts/13211446882/zip"])
    archive_sha = hashlib.sha256(raw).hexdigest()
    require(archive_sha == "a0b7d33f2e359f9e92eaa1dbb8b1a10735ff86d787b85caefdeb91a23beea426", "fixture archive differs")
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        original = json.loads(archive.read("recovery.json"))
    require(original["workload_verified"] and not original["verified"] and not original["workload_repeated"],
            "fixture source verdict differs")
    require(original["source"] == os.environ["PRODUCT"], "fixture source differs")
    compiler = original["phases"]["compiler"]
    require(compiler["cpp_summary"]["unresolved_tools"] == 2, "fixture unknown-tool scope differs")
    direct = compiler["indirect_links"][0]["wrapper_compile_producers"]
    require(len(direct) == 3, "fixture direct compiler inventory differs")
    base = {"invocation": original["invocation"], "counts": [],
            "actions": copy.deepcopy(compiler["compilers"] + compiler["local_go_actions"] + direct)}
    # These tiny synthetic headers exercise architecture and digest checks.
    # They are test data, never evidence of an executed compiler or real ELF.
    output_bytes = {}
    for action in base["actions"]:
        if action["owner"] in COWNERS:
            for output in action["outputs"]:
                data = b"\x7fELF\x02\x01" + bytes(12) + output["machine"].to_bytes(2, "little")
                output_bytes[output["path"]] = data
                output["digest"] = [hashlib.sha256(data).hexdigest(), len(data)]
    report = {"verified": False, "kind": "metadata_fixture_checks", "workload_executed": False,
              "fixture_archive": {"id": 13211446882, "sha256": archive_sha},
              "checker_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "cases": {}}
    expected_errors = {
        "direct": None,
        "unresolved_wrapper": "C++ action placement or input evidence incomplete",
        "empty_cpp": "no C++ action evidence",
        "missing_digest": "C++ action placement or input evidence incomplete",
        "wrong_placement": "C++ action placement or input evidence incomplete",
        "wrong_invocation": "execution log invocation differs",
        "failed_action": "C++ action placement or input evidence incomplete",
        "missing_embedded_owner": "missing cross-architecture compiler actions",
        "wrong_output_digest": "file bytes differ",
    }
    write("report-fixtures", report)
    for case in ("direct", "unresolved_wrapper", "empty_cpp", "missing_digest", "wrong_placement",
                 "wrong_invocation", "failed_action", "missing_embedded_owner", "wrong_output_digest"):
        value = copy.deepcopy(base)
        action = next(a for a in value["actions"] if a["mnemonic"] == "CppCompile")
        if case == "unresolved_wrapper":
            value["actions"].append(copy.deepcopy(compiler["indirect_links"][0]))
        elif case == "empty_cpp":
            value["actions"] = [a for a in value["actions"] if a["mnemonic"] not in {"CppCompile", "CppLink"}]
        elif case == "missing_digest":
            action["tools"][0]["digest"] = ["", 0]
        elif case == "wrong_placement":
            action["runner"] = "local"
        elif case == "wrong_invocation":
            value["invocation"] = "00000000-0000-0000-0000-000000000000"
        elif case == "failed_action":
            action["status"] = "EXECUTION_FAILED"
        elif case == "missing_embedded_owner":
            value["actions"].pop(0)
        elif case == "wrong_output_digest":
            value["actions"][0]["outputs"][0]["digest"][0] = "0" * 64
        (ROOT / "nogo.compact").write_text(json.dumps(value, sort_keys=True))
        accepted, error = False, None
        try:
            check("compiler", value, lambda item: output_bytes[item["path"]], base["invocation"])
            accepted = True
        except ValueError as exc:
            error = str(exc)
        data = (OUT / "compiler.json").read_bytes()
        result = json.loads(data)
        require(accepted == result["verified"] == (case == "direct") and error == expected_errors[case],
                "wrong fixture disposition: " + case)
        require(len(data) < 4 * 1024 * 1024, "fixture summary exceeded bound")
        report["cases"][case] = {"accepted": accepted, "error": error, "summary_bytes": len(data),
                                 "summary_sha256": hashlib.sha256(data).hexdigest()}
        write("report-fixtures", report)
    try:
        write("oversize-fixture", {"payload": "x" * (4 * 1024 * 1024)})
    except ValueError:
        require(not (OUT / "oversize-fixture.json").exists(), "oversized report was persisted")
    else:
        raise ValueError("oversized report was accepted")
    report.update(verified=True, oversize_rejected_before_write=True,
                  verdict_available=datetime.now(timezone.utc).isoformat())
    write("report-fixtures", report)


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["recover"]:
            recover()
        elif sys.argv[1:] == ["report-fixtures"]:
            report_fixtures()
        else:
            check(sys.argv[1])
    except Exception as exc:
        write(sys.argv[1] + "-error", {"verified": False, "error_type": type(exc).__name__,
                                      "error": str(exc) if isinstance(exc, ValueError) else "collection failed"})
        raise SystemExit(1)
