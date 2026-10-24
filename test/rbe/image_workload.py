"""Qualify six exported-module compilations in one remote Bazel build."""

import hashlib
import json
import os
import shlex
from pathlib import Path
import shutil
import zipfile

from image_execution import execution_records
import qualification_report as report


NETSTACK_PACKAGES = [
    "./pkg/tcpip", "./pkg/tcpip/adapters/gonet", "./pkg/tcpip/header",
    "./pkg/tcpip/link/channel", "./pkg/tcpip/network/ipv4",
    "./pkg/tcpip/network/ipv6", "./pkg/tcpip/stack",
    "./pkg/tcpip/transport/icmp", "./pkg/tcpip/transport/tcp",
    "./pkg/tcpip/transport/udp", "./pkg/buffer", "./pkg/waiter",
]
PLATFORMS = [
    ("darwin", "arm64"), ("windows", "amd64"), ("freebsd", "amd64"),
    ("openbsd", "amd64"), ("linux", "mips"), ("linux", "amd64"),
]


def argument(arguments: list[str], name: str) -> str:
    positions = [i for i, value in enumerate(arguments) if value == name]
    if len(positions) != 1 or positions[0] + 1 == len(arguments):
        raise ValueError(f"Expected one native {name} argument")
    return arguments[positions[0] + 1]


def digest_matches(item: dict, expected: dict) -> bool:
    return (item["digest"]["hash"] == expected["sha256"]
            and int(item["digest"].get("sizeBytes", 0)) == expected["bytes"])



COMPILER_TARGETS = [
    "//pkg/sentry/loader/vdsodata:vdso_arch",
    "//pkg/sentry/platform/systrap/sysmsg:sighandler_go_arch",
    "//pkg/sentry/platform/systrap/sysmsg:sighandler_binary_arch",
]
COMPILE_OWNERS = {"//vdso:vdso"} | {
    "//pkg/sentry/platform/systrap/sysmsg:" + name
    for name in ("sighandler", "syshandler", "sysrestorer", "sysmsg_lib")
}


def compiler_preflight(invoke, record_execution, evidence: Path, private: Path,
                       root: Path, options: list[str]) -> dict:
    execution = private / "compiler-preflight-execution.json"
    built = invoke("compiler-preflight", ["build", *options,
                   "--modify_execution_info=^(Genrule|ArchGenruleCp)$=+no-remote-cache",
                   "--execution_log_json_file=" + str(execution),
                   "--noexecution_log_sort", *COMPILER_TARGETS], "build", targets=COMPILER_TARGETS)
    inputs = []
    try:
        with execution.open(encoding="utf-8", newline="") as stream:
            for _, action in execution_records(stream):
                if action is None or action.get("targetLabel") not in COMPILE_OWNERS:
                    continue
                if action["mnemonic"] != "Genrule":
                    continue
                words = shlex.split(action["commandArgs"][-1])
                compilers = [word for word in words if word.startswith("external/")
                             and Path(word).name in ("clang", "clang++")]
                if len(compilers) != 1:
                    raise ValueError("Expected one declared clang compiler")
                compiler = compilers[0]
                selected = [item for item in action["inputs"] if item["path"] == compiler]
                if len(selected) != 1 or not selected[0]["digest"]["hash"]:
                    raise ValueError("Compiler is not an authenticated native action input")
                triple = argument(words, "-target")
                if triple not in ("x86_64-linux-gnu", "aarch64-linux-gnu"):
                    raise ValueError("Unexpected freestanding compiler target")
                inputs.append({"targetLabel": action["targetLabel"], "action_digest": action["digest"],
                               "triple": triple, "compiler": selected[0]})
    finally:
        record_execution(execution, "x86_64", "compiler-preflight")
    if built["status"] != "PASS" or built["native_exit"] != 0:
        raise RuntimeError("Declared compiler prerequisite build failed")
    proof = json.loads((evidence / "x86_64-compiler-preflight-image-execution.json").read_text())
    if proof["status"] != "PASS":
        raise ValueError("Compiler prerequisite physical action proof failed")
    actions = [row for row in proof["image_actions"]
               if row["targetLabel"] in COMPILE_OWNERS and row["mnemonic"] == "Genrule"]
    required = {(label, triple) for label in COMPILE_OWNERS
                for triple in ("x86_64-linux-gnu", "aarch64-linux-gnu")}
    if len(actions) != 10 or len(inputs) != 10 or {(row["targetLabel"], row["triple"]) for row in inputs} != required:
        raise ValueError("Expected all five real compiler owners in both architectures")
    for action in actions:
        if (action["runner"] != "remote" or action["cacheHit"] or action["remoteCacheable"]
                or action["exitCode"] != 0 or action["status"] or not action["remotable"]):
            raise ValueError("Compiler prerequisite must execute successfully and uncached on RBE")
        if sum(row["targetLabel"] == action["targetLabel"] and row["action_digest"] == action["digest"]
               for row in inputs) != 1:
            raise ValueError("Missing unique compiler action/input join")
    expected = built["expected"]
    if len(expected) != 3 or {row["label"] for row in expected} != set(COMPILER_TARGETS):
        raise ValueError("Unexpected split-architecture owners")
    owners = {(report.Owner(row["label"], row["configuration"]), "default") for row in expected}
    outputs = report.advertised_build_outputs(evidence / "invocation-compiler-preflight/events.jsonl", owners)
    if len(outputs) != 6 or any(sum(item["identity"]["label"] == label for item in outputs) != 2
                                for label in COMPILER_TARGETS):
        raise ValueError("Expected six split-architecture outputs")
    for output in outputs:
        name = Path(output["name"])
        if name.is_absolute() or ".." in name.parts or name.parts[:1] != ("bazel-out",):
            raise ValueError("Unsafe compiler output path")
        actual = report.file_record(root / name)
        if any(actual[key] != output[key] for key in ("bytes", "sha256")):
            raise ValueError("Materialized compiler output differs from configured BEP")
        matches = [action for action in proof["image_actions"]
                   if action["mnemonic"] == "ArchGenruleCp" and action["targetLabel"] == output["identity"]["label"]
                   and any(item["path"] == output["name"] and digest_matches(item, output)
                           for item in action["actualOutputs"])]
        if len(matches) != 1 or matches[0]["runner"] != "remote" or matches[0]["cacheHit"]:
            raise ValueError("Missing actual split-output copy action")
    return {"status": "PASS", "compiler_inputs": inputs, "outputs": outputs,
            "scope": "Ten remote compiler actions and six AMD64/ARM64 generated archive inputs."}


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    targets = config["targets"]
    module_labels = {f"//tools/go_export:{goos}_{goarch}" for goos, goarch in PLATFORMS}
    if len(targets) != 7 or set(targets) != module_labels | {"//:go_export"}:
        raise ValueError("Expected six module targets and their common source archive")
    options = [
        "--config=rbe", "--config=buildbuddy_remote_executor", "--config=x86_64",
        "--compilation_mode=opt", "--lockfile_mode=error", "--noannounce_rc",
        "--remote_download_outputs=all", "--build_event_json_file_path_conversion=false",
        "--spawn_strategy=remote", "--strategy=TestRunner=remote",
        "--noremote_local_fallback", "--jobs=400", "--build_tag_filters=",
        "--extra_toolchains=@llvm//toolchain:all",
    ]
    record = {"status": "INCOMPLETE", "source": config["source"],
              "public_source": config["public_source"],
              "scope": "Six exported Go module compilations; no target-OS runtime test."}
    try:
        result = invoke("execution-root", ["info", *options, "execution_root"], "command", targets=[])
        if result["status"] != "PASS":
            raise RuntimeError("Cannot identify the canonical execution root")
        root = Path((evidence / "invocation-execution-root/stdout.log").read_text().strip())
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("Invalid canonical execution root")
        record["compiler_preflight"] = compiler_preflight(invoke, record_execution, evidence, private, root, options)
        execution = private / "export-portability-execution.json"
        built = invoke("export-portability", ["build", *options,
                       "--modify_execution_info=^ExportedGoBuild$=+no-remote-cache", "--keep_going",
                       "--execution_log_json_file=" + str(execution),
                       "--noexecution_log_sort", *targets], "build", targets=targets)
        record["native_build_status"] = built["status"]
        if not execution.is_file():
            raise RuntimeError("Native exported-module execution log missing")
        # Retain only source/tool input identities here. The existing recorder
        # separately authenticates and redacts every full native action record.
        native_inputs = []
        gopath_records = []
        archive_gopath_inputs = []
        try:
            with execution.open(encoding="utf-8", newline="") as stream:
                for _, action in execution_records(stream):
                    if action is not None and action.get("targetLabel") == "//:gopath" and action.get("mnemonic") == "GoPath":
                        qualified_inputs = []
                        for output in record["compiler_preflight"]["outputs"]:
                            matches = [item for item in action["inputs"] if item["path"] == output["name"] and digest_matches(item, output)]
                            if len(matches) != 1:
                                raise ValueError("GoPath did not consume each qualified compiler output")
                            qualified_inputs.append(matches[0])
                        if argument(action["commandArgs"], "-mode") != "archive":
                            raise ValueError("Expected GoPath archive mode")
                        gopath_records.append({"action_digest": action["digest"], "inputs": qualified_inputs,
                                               "output_path": argument(action["commandArgs"], "-out")})
                    if action is not None and action.get("targetLabel") == "//:go_export" and action.get("mnemonic") == "Genrule":
                        words = shlex.split(action["commandArgs"][-1])
                        gopath = argument(words, "--gopath")
                        matches = [item for item in action["inputs"] if item["path"] == gopath]
                        if len(matches) != 1:
                            raise ValueError("Source archive omitted its actual GoPath archive input")
                        archive_gopath_inputs.append(matches[0])
                    if action is None or action.get("mnemonic") != "ExportedGoBuild":
                        continue
                    arguments = action["commandArgs"]
                    archive = argument(arguments, "--archive")
                    go = argument(arguments, "--go")
                    proxy = argument(arguments, "--proxy")
                    selected = [item for item in action["inputs"] if item["path"] in (archive, go)]
                    proxy_inputs = sorted((item for item in action["inputs"]
                                           if item["path"].startswith(proxy + "/")), key=lambda item: item["path"])
                    roots = [item for item in proxy_inputs if item["path"] == proxy + "/ROOT"]
                    modules = [item for item in proxy_inputs if item["path"].endswith(".mod")]
                    archives = [item for item in proxy_inputs if item["path"].endswith(".zip")]
                    if len(roots) != 1 or not modules or not archives:
                        raise ValueError("Missing declared offline proxy ROOT, module or ZIP inputs")
                    witnesses = [roots[0], modules[0], archives[0]]
                    for item in witnesses:
                        path = Path(item["path"])
                        if path.is_absolute() or ".." in path.parts:
                            raise ValueError("Unsafe native proxy input path")
                        if not digest_matches(item, report.file_record(root / path)):
                            raise ValueError("Materialized proxy input differs from actual action input")
                    proxy_summary = {"count": len(proxy_inputs), "module_count": len(modules),
                                     "zip_count": len(archives), "witnesses": witnesses,
                                     "sha256": hashlib.sha256(json.dumps(proxy_inputs, sort_keys=True,
                                                                        separators=(",", ":")).encode()).hexdigest()}
                    native_inputs.append({"targetLabel": action["targetLabel"],
                                          "action_digest": action["digest"],
                                          "archive": archive, "go": go, "proxy": proxy,
                                          "inputs": selected, "proxy_inputs": proxy_summary})
        finally:
            record_execution(execution, "x86_64", "export-portability")
        if built["status"] != "PASS" or built["native_exit"] != 0:
            raise RuntimeError("Exported module build failed")
        proof = json.loads((evidence / "x86_64-export-portability-image-execution.json").read_text())
        if proof["status"] != "PASS":
            raise ValueError("Native remote action proof failed")
        actions = [action for action in proof["image_actions"] if action["mnemonic"] == "ExportedGoBuild"]
        if len(actions) != 6 or len(native_inputs) != 6:
            raise ValueError("Expected exactly six native exported-module actions")
        if any(action["runner"] != "remote" or action["cacheHit"] or action["exitCode"] != 0
               or action["status"] or not action["remotable"] for action in actions):
            raise ValueError("All six module actions must execute successfully and uncached on RBE")
        expected = built["expected"]
        if len(expected) != 7 or {owner["label"] for owner in expected} != set(targets):
            raise ValueError("Unexpected configured build owners")
        owners = {(report.Owner(owner["label"], owner["configuration"]), "default") for owner in expected}
        outputs = report.advertised_build_outputs(evidence / "invocation-export-portability/events.jsonl", owners)
        if len(outputs) != 7 or {item["identity"]["label"] for item in outputs} != set(targets):
            raise ValueError("Expected seven unique configured default outputs")
        retained = evidence / "exports"
        retained.mkdir()
        for output in outputs:
            name = Path(output["name"])
            if name.is_absolute() or ".." in name.parts or name.parts[:1] != ("bazel-out",):
                raise ValueError("Unexpected configured output path")
            path = root / name
            actual = report.file_record(path)
            if any(actual[key] != output[key] for key in ("bytes", "sha256")):
                raise ValueError("Materialized output differs from configured default BEP identity")
            destination = retained / ("go_export.zip" if output["identity"]["label"] == "//:go_export"
                                      else output["identity"]["label"].split(":")[1] + ".json")
            shutil.copyfile(path, destination)
            output["retained"] = {"path": str(destination.relative_to(evidence)), **report.file_record(destination)}
        archive_output = next(item for item in outputs if item["identity"]["label"] == "//:go_export")
        archive_actions = [action for action in proof["image_actions"]
                           if action["targetLabel"] == "//:go_export" and action["mnemonic"] == "Genrule"]
        if len(archive_actions) != 1:
            raise ValueError("Missing unique source archive action")
        archive_action = archive_actions[0]
        matching = [item for item in archive_action["actualOutputs"] if item["path"] == archive_output["name"]]
        if len(matching) != 1 or not digest_matches(matching[0], archive_output):
            raise ValueError("Source archive actual output differs from default BEP identity")
        metadata = {}
        with zipfile.ZipFile(retained / "go_export.zip") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ValueError("Duplicate exported source paths")
            for name in names:
                path = Path(name)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("Unsafe exported source path")
            for name in ("go.mod", "go.sum"):
                data = archive.read(name)
                expected_metadata = config["module_metadata"][name]
                actual = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                if actual != expected_metadata or data != (Path.cwd() / name).read_bytes():
                    raise ValueError("Exported module metadata differs from the qualified source")
                metadata[name] = actual
        rows = []
        used_actions = set()
        sdk_identities = set()
        for goos, goarch in PLATFORMS:
            label = f"//tools/go_export:{goos}_{goarch}"
            output = next(item for item in outputs if item["identity"]["label"] == label)
            matches = [(index, action) for index, action in enumerate(actions)
                       if any(item["path"] == output["name"] and digest_matches(item, output)
                              for item in action["actualOutputs"])]
            if len(matches) != 1 or matches[0][0] in used_actions:
                raise ValueError("Configured module output must bind one distinct actual action")
            index, action = matches[0]
            used_actions.add(index)
            packages = ["./..."] if (goos, goarch) == ("linux", "amd64") else NETSTACK_PACKAGES
            expected_json = {"goos": goos, "goarch": goarch,
                             "cgo": (goos, goarch) == ("linux", "amd64"), "packages": packages}
            actual_json = json.loads((evidence / output["retained"]["path"]).read_text())
            if actual_json != expected_json:
                raise ValueError("Unexpected module compilation receipt")
            arguments = action["commandArgs"]
            if (argument(arguments, "--goos") != goos or argument(arguments, "--goarch") != goarch
                    or argument(arguments, "--output") != output["name"]
                    or arguments[-len(packages):] != packages
                    or ("--cc" in arguments) != expected_json["cgo"]):
                raise ValueError("Native module invocation differs from its configured receipt")
            inputs = [item for item in native_inputs if item["targetLabel"] == action["targetLabel"]
                      and item["action_digest"] == action["digest"]]
            if len(inputs) != 1:
                raise ValueError("Missing unique full native input record")
            selected = inputs[0]
            source_inputs = [item for item in selected["inputs"] if item["path"] == selected["archive"]]
            if (selected["archive"] != archive_output["name"] or len(source_inputs) != 1
                    or not digest_matches(source_inputs[0], archive_output)):
                raise ValueError("Module action did not consume the same authenticated source archive")
            go_inputs = [item for item in selected["inputs"] if item["path"] == selected["go"]]
            if len(go_inputs) != 1 or not go_inputs[0]["digest"]["hash"]:
                raise ValueError("Missing declared Go binary input")
            sdk_identities.add((selected["go"], go_inputs[0]["digest"]["hash"],
                                int(go_inputs[0]["digest"]["sizeBytes"])))
            rows.append({"owner": output["identity"], "receipt": actual_json,
                         "native_owner": action["targetLabel"], "action_digest": action["digest"],
                         "full_record_sha256": action["full_record_sha256"],
                         "declared_inputs": selected, "output": output})
        if used_actions != set(range(6)) or len(sdk_identities) != 1:
            raise ValueError("Expected six distinct actions sharing the same declared Go binary")
        gopath_actions = [action for action in proof["image_actions"]
                          if action["targetLabel"] == "//:gopath" and action["mnemonic"] == "GoPath"]
        if len(gopath_records) != 1 or len(gopath_actions) != 1 or len(archive_gopath_inputs) != 1:
            raise ValueError("Missing unique compiler-output/GoPath/archive-input chain")
        gopath = gopath_records[0]
        gopath_action = gopath_actions[0]
        gopath_input = archive_gopath_inputs[0]
        if (gopath["action_digest"] != gopath_action["digest"]
                or gopath["output_path"] != gopath_input["path"]
                or not any(item["path"] == gopath_input["path"] and item["digest"] == gopath_input["digest"]
                           for item in gopath_action["actualOutputs"])):
            raise ValueError("GoPath actual output differs from exported archive input")
        gopath_path = Path(gopath_input["path"])
        if gopath_path.is_absolute() or ".." in gopath_path.parts:
            raise ValueError("Unsafe GoPath output path")
        if not digest_matches(gopath_input, report.file_record(root / gopath_path)):
            raise ValueError("Materialized GoPath archive differs from native output/input identity")
        record.update(status="PASS", uncached_remote_module_actions=6,
                      gopath_preflight=gopath, archive_gopath_input=gopath_input,
                      native_gopath_action=gopath_action,
                      source_archive=archive_output, module_metadata=metadata,
                      exported_entries=len(names), modules=rows,
                      native_archive_action=archive_action)
    finally:
        report.write_json(evidence / "export-portability.json", record)
