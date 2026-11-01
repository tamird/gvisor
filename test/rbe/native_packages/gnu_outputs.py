"""Inspect ordinary GNU link inputs and the actual resulting ELF bytes.

The aquery path/input traversal and ELF reader are reused from the qualified
C++ utility carrier e1f9530768. No product rule or test invocation is changed.
"""

import collections
import datetime
import json
from pathlib import Path
import re
import subprocess

from static_build import elf


def gnu_links(graph, targets_expected):
    """Bind each ordinary binary to the supplier's declared GNU sysroot."""
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
        if target not in targets_expected:
            continue
        params = {p["execPath"]: p["arguments"] for p in action.get("paramFiles", [])}
        args = [value for arg in action["arguments"]
                for value in (params[arg[1:]] if arg.startswith("@") else [arg])]
        inputs = input_paths(action)
        search = [arg[2:] for arg in args if arg.startswith("-L") and len(arg) > 2]
        search.extend(args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "-L")
        # The link consumes a tree artifact; its producer declares the GNU
        # symbol stubs, linker scripts and nonshared supplement explicitly.
        directories = [name for name in search if name in inputs and name in producers
                       and targets[str(producers[name]["targetId"])].endswith("//runtimes/glibc:glibc_library_search_directory")]
        if len(directories) != 1:
            raise ValueError(target + ": GNU library search directory is not a declared, produced input")
        sysroot = {Path(name).name: name for name in input_paths(producers[directories[0]])}
        required = {"libc.so", "libc.so.6", "libc_nonshared.a"}
        if not required <= sysroot.keys():
            raise ValueError(target + ": GNU directory is missing declared C library inputs")
        outputs = [artifacts[str(i)] for i in action["outputIds"]
                   if Path(artifacts[str(i)]).name == target.split(":")[1]]
        if len(outputs) != 1:
            raise ValueError(target + ": expected one default executable output")
        links.append({"target": target, "action_key": action["actionKey"],
                      "libc_inputs": {name: sysroot[name] for name in sorted(required)},
                      "libc_search_directory": directories[0], "output": outputs[0],
                      "static_options": [arg for arg in args if arg in ("-static", "-static-pie")],
                      "execution_platform": action["executionPlatform"]})
    if collections.Counter(link["target"] for link in links) != collections.Counter(targets_expected):
        raise ValueError("The graph does not have exactly one link action for each original test")
    return {"status": "PASS", "links": links}


def collect(bazel, common, targets, tmp, arch):
    private = tmp / (arch + "-gnu-graph.json")
    log = tmp / (arch + "-gnu-graph.log")
    names = "|".join(target.split(":")[1] for target in targets)
    query = 'outputs(".*/(' + names + '|glibc_library_search_directory)$", deps(set(' + " ".join(targets) + ')))'
    with private.open("w") as output, log.open("w") as errors:
        code = subprocess.run(["timeout", "--signal=INT", "--kill-after=25s", "12m", *bazel,
                               "aquery", *common, "--include_param_files", "--output=jsonproto", query],
                              stdout=output, stderr=errors).returncode
    result = {"status": "INCOMPLETE", "graph_exit": code, "libc": "GNU", "tests_executed": 0,
              "graph_invocations": sorted(set(re.findall(r"https://app\.buildbuddy\.io/invocation/([0-9a-f-]{36})",
                                                         log.read_text(errors="replace"))))}
    if code:
        return result
    result.update(gnu_links(json.loads(private.read_text()), targets))
    # Build downloads its top-level cc_binary outputs; this reads those bytes,
    # not merely the digest advertised in BEP or an earlier cached receipt.
    execution_root = Path(subprocess.check_output([*bazel, "info", "execution_root"],
                                                  stderr=subprocess.DEVNULL, text=True).strip())
    result["outputs"] = []
    for link in result["links"]:
        binary = execution_root / link["output"]
        item = {"target": link["target"], "path": link["output"], "status": "INCOMPLETE"}
        try:
            item.update(elf(binary.read_bytes()))
            item["linkage"] = "dynamic" if item["interpreter"] or item["needed"] else "static"
            item["status"] = "PASS" if item["machine"] == {"amd64": 62, "arm64": 183}[arch] else "FAIL"
        except (OSError, ValueError) as err:
            item["collection_error"] = type(err).__name__ + ": " + str(err)[:400]
        result["outputs"].append(item)
    states = {item["status"] for item in result["outputs"]}
    result["status"] = "FAIL" if "FAIL" in states else "PASS" if states == {"PASS"} else "INCOMPLETE"
    result["verified_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return result
