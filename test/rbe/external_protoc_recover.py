"""Recover completed outputs after the original Bazel client exhausted memory."""

import base64
from collections import Counter
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
from zipfile import ZipFile

from cas_inputs import blob, cas, input_file, rpc
from external_go_policy import details, git, now

INV = "d7743a52-77c3-4b27-adda-be38912c4f38"
PRODUCT = "c734e2b4f7b776258294c4d5c2f508d6c6ec994e"
PARENT = "8813152889e4e5a41670dda3cb69b97f6e6c38b3"
BASE = "@@protobuf+//upb/base:base"
ABSL = "@@abseil-cpp+//absl/base:base"
PROTOC = "@@protobuf+//:protoc"
SHIM = "@@gvisor+//shim:containerd-shim-runsc-v1"
AREA = Path(os.environ["RUNNER_TEMP"]) / "external-go"
OUT = AREA.parent / "external-protoc-recovery.json"
PROPS = {"Arch": "amd64", "OSFamily": "linux", "EstimatedMemory": "2GB",
         "workload-isolation-type": "firecracker", "container-image": "docker://" + os.environ["GNU"]}
RESULT = {"verdict": "INCOMPLETE", "source": PRODUCT, "invocation": INV, "phases": {}}


def save():
    OUT.write_text(json.dumps(RESULT, indent=2) + "\n")


def phase(name, fn):
    row = RESULT["phases"][name] = {"status": "INCOMPLETE", "started": now()}
    save()
    try:
        value = fn(row)
        row["status"] = "PASS"
    except Exception as exc:
        value = None
        row["collection_error"] = type(exc).__name__
        tb = exc.__traceback__
        while tb.tb_next:
            tb = tb.tb_next
        row["collection_site"] = f"{Path(tb.tb_frame.f_code.co_filename).name}:{tb.tb_lineno}"
    row["finished"] = now()
    save()
    return value


def original(row):
    assert git("rev-parse", "HEAD^") == PARENT
    assert git("diff", "--name-only", PRODUCT, "HEAD") == ".github/workflows/build.yml\ntest/rbe/external_protoc.py\ntest/rbe/external_protoc_recover.py"
    data = (AREA / "original.zip").read_bytes()
    row["archive_sha256"] = sha256(data).hexdigest()
    assert row["archive_sha256"] == "e199eb663d5f469b20b402d86c9b750a5b9ae6fb90f7c2c64871c206c4d35f5a"
    with ZipFile(AREA / "original.zip") as archive:
        assert archive.namelist() == ["external-protoc.json"]
        previous = json.loads(archive.read("external-protoc.json"))
    assert previous["source"] == PRODUCT and previous["verdict"] == "FAIL"
    build = previous["phases"]["build"]
    assert build["invocation"] == INV and build["exit"] == 33 and build["source_unchanged"]
    assert build["bazel"] == "8.8.1" and build["completed"] == {"//:protoc": True, "//:shim": True, "//:extension": True}
    assert previous["phases"]["preflight"]["status"] == "PASS"
    row.update(run=41408846892, artifact=13126182561, original=previous,
               original_client_exit_name="OOM_ERROR", repeated_builds=0, repeated_queries=0)
    assert details("\n".join(build["errors"])) == []
    benign = "ERROR: linux/vfio.h: header not found"
    assert details(benign) == [benign]
    assert details("ERROR: --remote_header=x-buildbuddy-api-key=REDACTED") == []
    assert details("ERROR: Authorization: Bearer REDACTED") == []
    row["diagnostic_projection_controls"] = "PASS"
    return previous


def diagnostic(row):
    # The empty chunk ID asks for the last available chunk, not the large log.
    response = rpc("GetEventLogChunk", {"invocationId": INV, "minLines": 100})
    data = base64.b64decode(response.get("buffer", ""), validate=True)
    row.update(log_chunk_sha256=sha256(data).hexdigest(), log_chunk_bytes=len(data),
               diagnostics=details(data.decode(errors="replace")), live=response.get("live", False))
    assert len(data) <= 4 * 1024 * 1024



def prior_client_diagnostic(row):
    invocation = "0930b963-0517-4597-a33e-ef5d83b52ad4"
    row.update(invocation=invocation, chunks=[])
    chunks, token = [], "0"
    for _ in range(8):
        response = rpc("GetEventLogChunk", {"invocationId": invocation, "chunkId": token, "minLines": 100})
        data = base64.b64decode(response.get("buffer", ""), validate=True)
        row["chunks"].append({"sha256": sha256(data).hexdigest(), "size": len(data)})
        save()
        chunks.append(data)
        assert sum(map(len, chunks)) <= 4 * 1024 * 1024
        text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", b"".join(chunks).decode(errors="replace"))
        row["absolute_headers"] = sorted(set(re.findall(r"['\"](/usr/(?:include|lib)/[A-Za-z0-9_./+-]+)['\"]", text)))
        row["inclusion_owners"] = sorted(set(re.findall(r"absolute path inclusion\(s\) found in rule ['\"]([^'\"]+)['\"]", text)))
        row["diagnostics"] = details(text)
        save()
        if row["absolute_headers"] and BASE in row["inclusion_owners"]:
            row["absolute_inclusion_recovered"] = True
            return
        following = response.get("nextChunkId", "")
        if not data or not following:
            break
        assert following != token
        token = following
    row["absolute_inclusion_recovered"] = False


def executions(invocation, **selector):
    return rpc("GetExecution", {"executionLookup": {"invocationId": invocation, **selector},
                                 "inlineExecuteResponse": False}).get("execution", [])


def action(entry, invocation=INV):
    digest = entry["actionDigest"]
    matches = rpc("GetExecution", {"executionLookup": {"invocationId": invocation,
        "actionDigestHash": digest["hash"]}, "inlineExecuteResponse": True}).get("execution", [])
    matches = [e for e in matches if e["actionDigest"] == digest and
               e.get("targetLabel") == entry["targetLabel"] and e.get("actionMnemonic") == entry["actionMnemonic"]]
    assert len(matches) == 1, "Expected one original action result"
    e = matches[0]
    response = e["executeResponse"]
    result = response["result"]
    definition = cas(digest, "Action")
    command = cas(definition["commandDigest"], "Command")
    metadata = result.get("executionMetadata", e.get("executedActionMetadata", {}))
    row = {"owner": e["targetLabel"], "mnemonic": e["actionMnemonic"], "action": digest,
           "command": definition["commandDigest"], "input_root": definition["inputRootDigest"],
           "stage": e.get("stage"), "execution_status_code": e.get("status", {}).get("code", 0),
           "status_code": response.get("status", {}).get("code", 0),
           "exit": result.get("exitCode", 0), "cached": response.get("cachedResult", False),
           "invocation_link_type": e.get("invocationLinkType", 0),
           "timing": {k: v for k, v in metadata.items() if k.endswith("Timestamp")},
           "platform": {p["name"]: p["value"] for p in command.get("platform", {}).get("properties", []) if p["name"] in PROPS},
           "outputs": [{k: f[k] for k in ("path", "digest", "isExecutable") if k in f}
                       for f in result.get("outputFiles", [])]}
    return row, definition, command, result


def successful(row):
    assert row["stage"] in (4, "COMPLETED") and not row["execution_status_code"] and not row["status_code"] and not row["exit"]
    assert row["platform"] == PROPS


def owner(row, label, mnemonic):
    entries = [e for e in executions(INV, targetLabel=label) if e.get("targetLabel") == label and e.get("actionMnemonic") == mnemonic]
    row["matching_actions"] = len(entries)
    assert entries and len(entries) <= 32
    if mnemonic == "CppCompile":
        entries = sorted(entries, key=lambda e: e.get("primaryOutputPath", ""))[:1]
    row["actions"] = []
    recovered = []
    for e in entries:
        record, definition, command, result = action(e)
        row["actions"].append(record)
        save()
        if record["exit"] or record["status_code"] or record["execution_status_code"]:
            row["status"] = "FAIL"
        successful(record)
        if mnemonic == "CppCompile":
            args = command["arguments"]
            assert args[0].startswith("external/llvm+") and args[0].endswith(("/clang", "/clang++"))
            assert len(json.dumps(args)) < 32768
            assert not any(re.search(r"(?:^|=)/usr/(?:include|lib)", a) or re.match(r"-(?:I|isystem)/usr/", a) for a in args)
            compiler = input_file(definition["inputRootDigest"], args[0])
            record.update(arguments=args, compiler={k: compiler[k] for k in ("name", "digest", "isExecutable") if k in compiler})
        recovered.append((record, definition, command, result))
    return recovered


def output(row, records, basename, flag, expected):
    row["binaries"] = []
    matches = [(record, f) for record, _, _, _ in records for f in record["outputs"] if Path(f["path"]).name == basename]
    assert matches and len(matches) <= 4
    for index, (record, f) in enumerate(matches):
        item = {"owner": record["owner"], "action": record["action"], "path": f["path"], "digest": f["digest"],
                "has_exec_configuration_marker": "-exec-" in f["path"], "cached": record["cached"]}
        row["binaries"].append(item)
        save()
        data = blob(f["digest"], INV)
        assert data[:6] == b"\x7fELF\x02\x01" and int.from_bytes(data[18:20], "little") == 62
        path = AREA / f"recovered-{basename}-{index}"
        path.write_bytes(data)
        path.chmod(0o555)
        elf = subprocess.run(["readelf", "-l", "-d", str(path)], capture_output=True, text=True, check=True).stdout
        item.update(elf_machine=62, interpreter=re.findall(r"Requesting program interpreter: ([^\]]+)", elf),
                    needed=re.findall(r"\(NEEDED\).*\[([^\]]+)\]", elf))
        p = subprocess.run([str(path), flag], capture_output=True, text=True, timeout=30,
                           env={k: v for k, v in os.environ.items() if k not in ("BUILDBUDDY_API_KEY", "GH_TOKEN")})
        matches_output = p.stdout.strip() == expected if flag == "--version" else p.stdout.startswith(expected)
        item.update(exit=p.returncode, expected_output=matches_output,
                    version_line=p.stdout.strip() if re.fullmatch(r"libprotoc [0-9A-Za-z.+-]+", p.stdout.strip()) else None,
                    output_sha256=sha256((p.stdout + p.stderr).encode()).hexdigest(), completed=now())
        if p.returncode:
            row["status"] = "FAIL"
        save()
        assert p.returncode == 0 and matches_output
    assert any(not item["has_exec_configuration_marker"] for item in row["binaries"])


def consumers(row, links):
    inventory = executions(INV)
    assert len(inventory) <= 20000
    row["execution_count"] = len(inventory)
    row["mnemonic_counts"] = dict(sorted(Counter(e.get("actionMnemonic", "") for e in inventory).items()))
    candidates = [e for e in inventory if e.get("actionMnemonic") in ("GenProtoDescriptorSet", "GoProtocGen")
                  and "com_github_containerd_" in e.get("targetLabel", "")]
    row.update(matching_actions=len(candidates), observations=[])
    # One actual generator is enough to establish use, if such an action exists.
    for e in sorted(candidates, key=lambda e: (e["actionMnemonic"], e["targetLabel"]))[:1]:
        record, definition, command, _ = action(e)
        row["observations"].append(record)
        save()
        successful(record)
        args = command["arguments"][:]
        for arg in args[:]:
            if arg.startswith("-param=") or arg.startswith("@"):
                path = arg[len("-param="):] if arg.startswith("-param=") else arg[1:]
                file = input_file(definition["inputRootDigest"], path)
                args.extend(shlex.split(blob(file["digest"], INV).decode()))
        paths = sorted({a.split("=", 1)[-1] for a in args if a.split("=", 1)[-1].endswith("/protoc")})
        assert len(paths) == 1, "Expected actual protoc argv path"
        path = paths[0]
        file = input_file(definition["inputRootDigest"], path)
        matching = [(link, f) for link, _, _, _ in links for f in link["outputs"] if f["path"] == path and f["digest"] == file["digest"]]
        assert len(matching) == 1 and "-exec-" in path, "Generator tool must match compiled exec-config protoc"
        record.update(protoc_path=path, protoc_digest=file["digest"], cpp_link_action=matching[0][0]["action"])
    row["source_protoc_use"] = "OBSERVED_IN_INVOCATION" if row["observations"] else "UNVERIFIED_NO_MATCHING_EXECUTIONS"
    row["specific_shim_or_extension_dependency"] = "NOT_IDENTIFIED"


def prior_compiler(row):
    # The previous original action identities are already public and immutable.
    invocation = "0930b963-0517-4597-a33e-ef5d83b52ad4"
    row.update(invocation=invocation, original_client_exit=1, actions=[])
    for label, digest_hash, expected_exit in ((BASE, "805251d8b2f0b13353c41d0da61f79b76aeaaf5f79a0b4fa4ad8b5a9477c033c", 0),
            (ABSL, "a5d2334537937ec556c353a75695cc7b47136b80dbb42d20c1378cfa22e0aa0b", 1)):
        entries = [e for e in executions(invocation, actionDigestHash=digest_hash)
                   if e.get("targetLabel") == label and e["actionDigest"]["hash"] == digest_hash]
        assert len(entries) == 1
        record, _, command, result = action(entries[0], invocation)
        row["actions"].append(record)
        save()
        assert record["exit"] == expected_exit and command["arguments"][0] == "/usr/bin/x86_64-linux-gnu-gcc"
        record["arguments"] = command["arguments"]
        if "stderrDigest" in result and int(result["stderrDigest"].get("sizeBytes", 0)):
            raw = blob(result["stderrDigest"], invocation)
        elif "stderrDigest" in result:
            raw = b""
            assert result["stderrDigest"]["hash"] == sha256(raw).hexdigest()
        else:
            raw = base64.b64decode(result.get("stderrRaw", ""), validate=True)
        record.update(stderr_sha256=sha256(raw).hexdigest(), diagnostics=details(raw.decode(errors="replace")))
        save()


def main():
    if phase("original", original) is not None:
        phase("last_log_chunk", diagnostic)
        phase("original_system_compiler", prior_compiler)
        phase("original_client_diagnostic", prior_client_diagnostic)
        phase("upb_compile", lambda row: owner(row, BASE, "CppCompile"))
        phase("abseil_compile", lambda row: owner(row, ABSL, "CppCompile"))
        protoc = phase("protoc_links", lambda row: owner(row, PROTOC, "CppLink"))
        shim = phase("shim_link", lambda row: owner(row, SHIM, "GoLink"))
        if protoc is not None:
            phase("protoc_cli", lambda row: output(row, protoc, "protoc", "--version", "libprotoc 35.1"))
            phase("source_protoc_consumers", lambda row: consumers(row, protoc))
        if shim is not None:
            phase("shim_cli", lambda row: output(row, shim, "containerd-shim-runsc-v1", "-v", "containerd-shim-runsc-v1:"))
    required = ("original", "upb_compile", "abseil_compile", "protoc_links", "shim_link", "protoc_cli", "shim_cli", "source_protoc_consumers")
    complete = all(RESULT["phases"].get(p, {}).get("status") == "PASS" for p in required)
    observed = RESULT["phases"].get("source_protoc_consumers", {}).get("source_protoc_use") == "OBSERVED_IN_INVOCATION"
    RESULT["verdict"] = ("RECOVERED" if observed else "OUTPUTS_RECOVERED_CONSUMER_UNVERIFIED") if complete else "INCOMPLETE"
    if any(row["status"] == "FAIL" for row in RESULT["phases"].values()):
        RESULT["verdict"] = "FAIL"
    RESULT.update(original_client_status="FAIL", original_client_exit=33, finished=now())
    save()
    return RESULT["verdict"] not in ("RECOVERED", "OUTPUTS_RECOVERED_CONSUMER_UNVERIFIED")


if __name__ == "__main__":
    raise SystemExit(main())
