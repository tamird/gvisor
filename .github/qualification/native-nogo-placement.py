"""Check the native Nogo worker and retain actual cross-compiler placement."""

from collections import Counter
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
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
    (OUT / (name + ".json")).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


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


def inspect(path):
    files, inputs, selected = {}, {}, []
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
                   "digest": digest(first(spawn, 16)), "platform": {k: props.get(k) for k in PROPS},
                   "argv_sha256": hashlib.sha256(json.dumps(args).encode()).hexdigest(),
                   "tools": [{"path": tool, "digest": declared.get(tool)} for tool in tools],
                   "target_triples": sorted(set(re.findall(r"(?:x86_64|aarch64)[-a-z0-9]*linux[-a-z0-9]*", " ".join(args)))),
                   "outputs": outputs}
            selected.append(row)
        require(process.wait() == 0, "incomplete compact log")
    return {"invocation": invocation, "actions": selected,
            "counts": [{"mnemonic": k[0], "runner": k[1], "cached": k[2], "count": v}
                       for k, v in sorted(counts.items())]}


def check(mode):
    report = inspect(ROOT / ("probe.compact" if mode == "probe" else "nogo.compact"))
    report.update(verified=False, source=os.environ["PRODUCT"])
    write(mode, report)
    rows = report["actions"]
    if mode == "probe":
        require(len(rows) == 1, "missing unique worker probe")
        row, = rows
        require(row["owner"].endswith("//:no_compiler") and row["exit"] == 0 and not row["cached"]
                and row["runner"] == "remote" and row["platform"] == PROPS, "worker probe did not execute on the pinned image")
        output, = row["outputs"]
        data = Path(output["path"]).read_bytes()
        require(data == b"no host compiler; bash and awk available\n" and hashlib.sha256(data).hexdigest() == output["digest"][0], "wrong probe output")
    else:
        verdict = json.loads((OUT / "verdict.json").read_text())
        require(report["invocation"] == verdict["invocation"], "execution log invocation differs")
        compiled = [row for row in rows if row["owner"] in COWNERS]
        require(Counter(row["owner"] for row in compiled) == {owner: 2 for owner in COWNERS}, "missing cross-architecture compiler actions")
        for row in compiled:
            require(row["runner"] == "remote" and not row["cached"] and row["exit"] == 0
                    and row["platform"] == PROPS, "compiler action placement or result differs")
            require(row["tools"] and all(tool["digest"] for tool in row["tools"]), "compiler or linker is not a declared input")
            require(any("/clang" in tool["path"] for tool in row["tools"]) and row["target_triples"], "missing declared compiler or target triple")
            for output in row["outputs"]:
                data = Path(output["path"]).read_bytes()
                require(data[:6] == b"\x7fELF\x02\x01" and hashlib.sha256(data).hexdigest() == output["digest"][0], "wrong materialized compiler output")
                output["machine"] = int.from_bytes(data[18:20], "little")
                require(output["machine"] in {62, 183}, "wrong embedded ELF architecture")
        for owner in COWNERS:
            require({output["machine"] for row in compiled if row["owner"] == owner for output in row["outputs"]} == {62, 183}, "missing one C target architecture")
        for row in rows:
            if row["mnemonic"] in {"CppCompile", "CppLink"}:
                require(row["runner"] == "remote" and row["platform"] == PROPS
                        and row["tools"] and all(tool["digest"] for tool in row["tools"]), "host compiler dependency")
        local = [row for row in rows if row["owner"] == "//runsc/cgroup:cgroup_test"
                 and row["mnemonic"] in {"GoCompilePkg", "GoStaticAnalysis"}]
        require(all(row["runner"] == "local" and row["exit"] == 0 for row in local)
                and {row["mnemonic"] for row in local} == {"GoCompilePkg", "GoStaticAnalysis"}, "missing explicit cgroup Go-only placement")
    report.update(verified=True, verdict_available=datetime.now(timezone.utc).isoformat())
    write(mode, report)


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
        keys = ("verified", "source", "host", "status", "bazel_exit", "invocation",
                "configured", "testRunners", "summaries", "amd64Markers", "arm64Markers",
                "expected", "missing", "unexpected", "skipped", "agent_options",
                "execution_finished", "verdict_available", "started", "finished", "processes")
        row.update(log_sha256=hashlib.sha256(raw).hexdigest(), log_bytes=len(raw),
                   original_verdict={key: value[key] for key in keys},
                   original_projection_step="failure", original_upload_gate="filename/type/8MiB bound")

    def valid_digest(value):
        return (isinstance(value, list) and len(value) == 2
                and isinstance(value[0], str) and re.fullmatch(r"[a-f0-9]{64}", value[0])
                and isinstance(value[1], int) and value[1] > 0)

    records = {}

    def events(row):
        result = rpc("GetInvocation", {"lookup": {"invocationId": invocation}})
        inv, = result["invocation"]
        require(inv["invocationId"] == invocation, "different invocation returned")
        entries = [entry["buildEvent"] for entry in inv["event"]]
        results = [entry for entry in entries if "testResult" in entry]
        expected = set(report["phases"]["original"]["original_verdict"]["expected"])
        selected = {}
        for entry in results:
            identity, value = entry["id"]["testResult"], entry["testResult"]
            label = identity["label"]
            require(label not in selected, "duplicate original test attempt")
            execution = value.get("executionInfo", {})
            selected[label] = {"identity": identity, "status": {1: "PASSED", "PASSED": "PASSED"}.get(value.get("status"), value.get("status")),
                               "cachedLocally": value.get("cachedLocally", False),
                               "cachedRemotely": execution.get("cachedRemotely", False),
                               "strategy": execution.get("strategy")}
        row["attempts"] = selected
        require(set(selected) == expected and len(selected) == 871, "original owner set differs")
        require(all(value["status"] == "PASSED" and not value["cachedLocally"]
                    and not value["cachedRemotely"] and value["strategy"] == "processwrapper-sandbox"
                    for value in selected.values()), "original test execution differs")
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
        compiled = [action for action in value["actions"] if action["owner"] in COWNERS]
        local = [action for action in value["actions"] if action["owner"] == "//runsc/cgroup:cgroup_test"
                 and action["mnemonic"] in {"GoCompilePkg", "GoStaticAnalysis"}]
        row.update(compilers=compiled, local_go_actions=local,
                   original_two_actions_per_owner=Counter(a["owner"] for a in compiled) == {owner: 2 for owner in COWNERS})
        cpp = [action for action in value["actions"] if action["mnemonic"] in {"CppCompile", "CppLink"}]
        row["cpp_summary"] = {"actions": len(cpp), "mnemonics": dict(Counter(a["mnemonic"] for a in cpp)),
                              "wrong_placement": sum(a["runner"] != "remote" or a["platform"] != PROPS for a in cpp),
                              "missing_compact_tool_digest": sum(not a["tools"] or not all(valid_digest(t["digest"]) for t in a["tools"]) for a in cpp),
                              "rows_sha256": hashlib.sha256(json.dumps(cpp, sort_keys=True).encode()).hexdigest()}
        row["cpp_issue_examples"] = [a for a in cpp if a["runner"] != "remote" or a["platform"] != PROPS
                                     or not a["tools"] or not all(valid_digest(t["digest"]) for t in a["tools"])][:8]
        save()
        require(set(a["owner"] for a in compiled) == COWNERS and 10 <= len(compiled) <= 40,
                "incomplete or unbounded embedded compiler selection")
        require(len({tuple(a["digest"]) for a in compiled}) == len(compiled), "duplicate compiler action")
        for action in compiled:
            require(action["runner"] == "remote" and not action["cached"] and action["exit"] == 0
                    and action["platform"] == PROPS, "embedded compiler execution differs")
            require(action["tools"] and all(valid_digest(tool["digest"]) for tool in action["tools"]),
                    "embedded compiler input digest missing from original compact log")
            require(any("/clang" in tool["path"] for tool in action["tools"]) and action["target_triples"],
                    "missing declared embedded compiler or target triple")
            require(action["outputs"], "missing embedded compiler output")
            for output in action["outputs"]:
                data = blob({"hash": output["digest"][0], "sizeBytes": output["digest"][1]}, invocation)
                require(data[:6] == b"\x7fELF\x02\x01", "wrong original embedded ELF")
                output["machine"] = int.from_bytes(data[18:20], "little")
                require(output["machine"] in {62, 183}, "wrong original embedded ELF architecture")
            save()
        for owner in COWNERS:
            require({o["machine"] for a in compiled if a["owner"] == owner for o in a["outputs"]} == {62, 183},
                    "missing embedded target architecture")
        require(cpp and not row["cpp_summary"]["wrong_placement"] and not row["cpp_summary"]["missing_compact_tool_digest"],
                "C++ action placement or input evidence incomplete")
        require(local and all(a["runner"] == "local" and a["exit"] == 0 for a in local)
                and {a["mnemonic"] for a in local} == {"GoCompilePkg", "GoStaticAnalysis"},
                "explicit local Go placement differs")

    phase("original", receipt)
    phase("original_events", events)
    phase("compiler", compiler)
    report["verdict_available"] = datetime.now(timezone.utc).isoformat()
    report["verified"] = all(p["verified"] for p in report["phases"].values())
    report["workload_verified"] = report["phases"]["original"]["verified"]
    save()
    raise SystemExit(0 if report["verified"] else 1)


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["recover"]:
            recover()
        else:
            check(sys.argv[1])
    except Exception as exc:
        write(sys.argv[1] + "-error", {"verified": False, "error_type": type(exc).__name__,
                                      "error": str(exc) if isinstance(exc, ValueError) else "collection failed"})
        raise SystemExit(1)
