"""Finish the original goimports proof without repeating its lister controls."""

from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import zipfile

import check as q
from cas_inputs import blob, cas, input_file, rpc

os.environ.pop("BUILDBUDDY_API_KEY", None)
ORIGINAL = 13120871828
ARCHIVE = "e606eb34b7c7b8c00598bc3c8d545925803b3fb80357d96bdb71a65f3ecdc9e5"
INVOCATION = "f4a40b3c-5a04-4cf2-8580-6505fdf51ac5"


def original() -> None:
    path = q.TEMP / "goimports-original.zip"
    q.require(sha256(path.read_bytes()).hexdigest() == ARCHIVE, "original archive differs")
    with zipfile.ZipFile(path) as z:
        q.require(z.namelist() == ["goimports-bazel9.json"], "unexpected original members")
        prior = json.loads(z.read("goimports-bazel9.json"))
    q.require(prior["source"] == q.SOURCE and prior["tree"] == q.TREE and prior["verdict"] == "INCOMPLETE", "wrong original source")
    expected = {"input_graph", "clean", "new_package", "invalid_syntax", "format", "formatted", "removed"}
    q.require(set(prior["phases"]) == expected | {"minimal_image"}
              and all(prior["phases"][p]["verified"] for p in expected), "original controls incomplete")
    prior["originalCollection"] = {"run": 41398915499, "artifact": ORIGINAL, "archiveSha256": ARCHIVE,
                                   "verdict": prior["verdict"], "error": prior.pop("error"),
                                   "errorLine": prior.pop("errorLine"), "verifiedAt": prior.pop("verifiedAt")}
    q.OUT.clear()
    q.OUT.update(prior)
    q.save()


def original_elf() -> None:
    row = q.OUT["phases"]["minimal_image"]
    q.require(row["invocation"] == INVOCATION and row["exit"] == 0 and row["readOnly"]
              and row["completed"] == [q.TARGET] and len(row["actions"]) == 1, "wrong original lister result")
    witness, = row["actions"]
    q.require(witness["owner"] == "//tools/lint:goimports_list" and witness["mnemonic"] == "GoimportsList"
              and witness["runner"] == "remote" and not witness["cache"] and not witness["exit"] and not witness["status"], "wrong original action")
    invs = rpc("GetInvocation", {"lookup": {"invocationId": INVOCATION}}).get("invocation", [])
    inv, = [i for i in invs if i.get("invocationId") == INVOCATION]
    finish, = [e["buildEvent"]["finished"] for e in inv.get("event", []) if "finished" in e.get("buildEvent", {})]
    stamp = finish.get("finishTimeMillis")
    if stamp is None:
        stamp = round(datetime.fromisoformat(finish["finishTime"]).timestamp() * 1000)
    q.require(finish.get("overallSuccess") and int(stamp) == round(datetime.fromisoformat(row["bepFinished"][0]["time"]).timestamp() * 1000), "original finish differs")
    rows = rpc("GetExecution", {"executionLookup": {"invocationId": INVOCATION}, "inlineExecuteResponse": True}).get("execution", [])
    selected = [e for e in rows if e.get("targetLabel") == "//tools/lint:goimports_list" and e.get("actionMnemonic") == "GoimportsList"]
    row["recoveredActionMatches"] = len(selected)
    q.save()
    execution, = selected
    response = execution.get("executeResponse", {})
    q.require(execution.get("stage") in (4, "COMPLETED") and not execution.get("status", {}).get("code", 0)
              and "result" in response and not response.get("status", {}).get("code", 0)
              and not response.get("cachedResult", False) and not response["result"].get("exitCode", 0), "original execution not successful")
    digest = execution["actionDigest"]
    action = cas(digest, "Action")
    command = cas(action["commandDigest"], "Command")
    props = {p["name"]: p["value"] for p in (action.get("platform") or command.get("platform", {})).get("properties", [])}
    q.require(all(props.get(k) == v for k, v in witness["platform"].items())
              and props.get("container-image") == q.NATIVE and props.get("workload-isolation-type") == "oci"
              and props.get("recycle-runner") == "false", "original worker differs")
    args = command["arguments"]
    start = args.index("-format-only")
    tool = args[start - 1]
    sources = {p.decode() for p in subprocess.check_output(["git", "ls-files", "-z", "*.go"]).split(b"\0") if p} - {str(q.GOLDEN)}
    listed = [p.split("/ROOT.d/", 1)[1] for p in args[start + 3:] if "/ROOT.d/" in p]
    q.require(args[start:start + 3] == ["-format-only", "-l", "--"] and listed == sorted(sources)
              and len(listed) == len(args[start + 3:]) and tool == q.OUT["phases"]["clean"]["tool"], "original command differs")
    report, = [f for f in response["result"].get("outputFiles", []) if f["path"].endswith("/goimports.list")]
    q.require(report["digest"]["hash"] == sha256(b"").hexdigest() and int(report["digest"].get("sizeBytes", 0)) == 0, "original report not empty")
    node = input_file(action["inputRootDigest"], tool)
    data = blob(node["digest"], INVOCATION)
    path = q.TEMP / "lint-bazel9/original-goimports"
    path.write_bytes(data)
    elf = subprocess.check_output(["readelf", "-h", "-l", "-d", str(path)], text=True)
    row["minimalTool"] = {"sha256": sha256(data).hexdigest(), "size": len(data),
                          "static": "INTERP" not in elf and "NEEDED" not in elf,
                          "amd64": "Advanced Micro Devices X86-64" in elf}
    row.update(recoveredAction=digest, processedSources=len(listed), goldenExcluded=True,
               filenameReport=report["digest"], reusedValidatorPhase="clean", newListerExecutions=0)
    q.save()
    q.require(row["minimalTool"]["static"] and row["minimalTool"]["amd64"], "original tool is not static AMD64")
    row["verified"] = True
    row["recoveredAt"] = q.now()


def phase(name, function) -> None:
    try:
        function()
    except Exception as e:
        q.OUT.setdefault("completionErrors", {})[name] = {"type": type(e).__name__}
    finally:
        q.save()


head = subprocess.check_output(["git", "rev-parse", "HEAD", "HEAD^{tree}"], text=True).splitlines()
q.require(head == [q.SOURCE, q.TREE] and not subprocess.check_output(["git", "status", "--porcelain"]), "not the clean product")
phase("original_receipt", original)
phase("integrated", q.integrated)
phase("original_elf", original_elf)
q.OUT["completionSourceClean"] = not subprocess.check_output(["git", "status", "--porcelain"])
expected = {"input_graph", "clean", "new_package", "invalid_syntax", "format", "formatted", "removed", "minimal_image", "integrated"}
ok = (not q.OUT.get("completionErrors") and q.OUT["completionSourceClean"]
      and set(q.OUT["phases"]) == expected and all(p["verified"] for p in q.OUT["phases"].values()))
q.OUT["verdict"] = "PASS" if ok else "FAIL" if q.OUT["phases"].get("integrated", {}).get("exit", 0) else "INCOMPLETE"
q.OUT["verifiedAt"] = q.now()
q.save()
raise SystemExit(not ok)
