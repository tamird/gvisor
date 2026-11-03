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


if __name__ == "__main__":
    try:
        check(sys.argv[1])
    except Exception as exc:
        write(sys.argv[1] + "-error", {"verified": False, "error_type": type(exc).__name__,
                                      "error": str(exc) if isinstance(exc, ValueError) else "collection failed"})
        raise SystemExit(1)
