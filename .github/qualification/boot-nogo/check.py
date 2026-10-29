"""Read the actual boot Nogo test and its two freestanding dependencies."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit


ROOT = Path(os.environ["RUNNER_TEMP"]) / "boot-nogo"
TARGET = "//runsc/boot:boot_nogo"
PRODUCERS = {"//vdso:vdso.so", "//pkg/sentry/platform/systrap/sysmsg:sighandler.built-in.bin.o"}


def file_info(path):
    data = path.read_bytes()
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def reconcile():
    code = int((ROOT / "exit").read_text())
    output = {"source": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
              "bazel_exit": code, "links": [], "compiles": {}, "boot_analysis_outputs": [], "failures": [], "test": None}
    bep = ROOT / "events.jsonl"
    execroot = Path((ROOT / "execroot").read_text().strip())
    for line in bep.open() if bep.exists() else ():
        event = json.loads(line)
        ids = event["id"]
        if "started" in event:
            output["child"] = event["started"]["uuid"]
        if "finished" in event:
            output["build_finished"] = event["finished"]["exitCode"]["name"]
        action = event.get("action")
        if action:
            aid = ids["actionCompleted"]
            owner, mnemonic = aid.get("label"), action.get("type")
            if not action.get("success"):
                if len(output["failures"]) < 8:
                    output["failures"].append({"owner": owner, "mnemonic": mnemonic, "exit": action.get("exitCode"),
                                               "why": action.get("failureDetail", {}).get("message", "")[:540]})
                continue
            if owner == "//runsc/boot:boot" and mnemonic == "GoStaticAnalysis":
                output["boot_analysis_outputs"].append(aid.get("primaryOutput"))
            if owner not in PRODUCERS or mnemonic not in {"CppCompile", "CppLink"}:
                continue
            argv = action.get("commandLine", [])
            path = Path(aid["primaryOutput"])
            tool = argv[0] if argv else ""
            if mnemonic == "CppCompile":
                key = f"{owner} {tool}"
                output["compiles"][key] = output["compiles"].get(key, 0) + 1
            elif (exe := execroot / path).is_file():
                with exe.open("rb") as stream:
                    ident = stream.read(20)
                output["links"].append({"owner": owner, "tool": tool, "path": str(path),
                                        "machine": int.from_bytes(ident[18:20], "little") if ident.startswith(b"\x7fELF") else None,
                                        **file_info(exe)})
        if ids.get("testResult", {}).get("label") == TARGET:
            result = event["testResult"]
            test = output["test"] = {"status": result["status"], "strategy": result.get("executionInfo", {}).get("strategy"),
                                     "outputs": {}, "arches": []}
            for entry in result.get("testActionOutput", []):
                name = entry["name"]
                if name not in {"test.log", "test.xml"}:
                    continue
                uri = urlsplit(entry.get("uri", ""))
                path = Path(unquote(uri.path)) if uri.scheme == "file" else Path("bazel-testlogs/runsc/boot/boot_nogo") / name
                if not path.is_file():
                    continue
                info = file_info(path)
                info["matches_bep"] = (not entry.get("digest") or entry["digest"] == info["sha256"]) and (
                    not entry.get("length") or int(entry["length"]) == info["size"])
                test["outputs"][name] = info
                if name == "test.log":
                    test["arches"] = sorted(re.findall(r"(?m)^(amd64|arm64)\.\.\.PASS\s*$", path.read_text()))
    lines = (ROOT / "native.log").read_text(errors="replace").splitlines()
    error_indices = [i for i, line in enumerate(lines) if line.startswith("ERROR:")]
    output["first_errors"] = [lines[i][:550] for i in error_indices[:5]]
    if error_indices:
        i = error_indices[0]
        output["first_error_context"] = [line[:550] for line in lines[i:i+24]]
    test = output["test"] or {}
    tool_ok = all(any(k.startswith(label+" ") and "clang" in k and "/usr/bin/" not in k for k in output["compiles"]) for label in PRODUCERS)
    output["verified"] = (code == 0 and output.get("build_finished") == "SUCCESS" and test.get("status") == "PASSED"
                          and test.get("arches") == ["amd64", "arm64"] and set(test.get("outputs", {})) == {"test.log", "test.xml"}
                          and all(v["matches_bep"] for v in test["outputs"].values()) and tool_ok
                          and all({x["machine"] for x in output["links"] if x["owner"] == label} >= {62, 183} for label in PRODUCERS))
    print(json.dumps(output, indent=2))
    return output["verified"]


if __name__ == "__main__":
    sys.exit(0 if reconcile() else 1)
