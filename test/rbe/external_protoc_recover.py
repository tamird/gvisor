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
PARENT = "bf46dc5c4b24a23f49dd44b571ec1daf13426522"
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
        directory = AREA / "binaries" / str(index)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / basename
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


def inventory(row, previous):
    entries = executions(INV)
    assert len(entries) <= 20000
    row["execution_count"] = len(entries)
    row["mnemonic_counts"] = dict(sorted(Counter(e.get("actionMnemonic", "") for e in entries).items()))
    row["generators"] = [{k: e.get(k) for k in ("targetLabel", "actionMnemonic", "actionDigest", "primaryOutputPath")}
                         for e in entries if e.get("actionMnemonic") in ("GenProtoDescriptorSet", "GoProtocGen")]
    expected = previous["phases"]["source_protoc_consumers"]["mnemonic_counts"]
    row["expected_generator_counts"] = {m: expected[m] for m in ("GenProtoDescriptorSet", "GoProtocGen")}
    assert {m: row["mnemonic_counts"].get(m, 0) for m in row["expected_generator_counts"]} == row["expected_generator_counts"]
    digests = [(e["actionDigest"]["hash"], e["actionDigest"]["sizeBytes"]) for e in row["generators"]]
    assert len(set(digests)) == len(digests)
    return entries


def consumers(row, links, inventory):
    candidates = [e for e in inventory if e.get("actionMnemonic") in ("GenProtoDescriptorSet", "GoProtocGen")]
    assert len(candidates) <= 8
    row.update(matching_actions=len(candidates), observations=[])
    for e in sorted(candidates, key=lambda e: (e["actionMnemonic"], e["targetLabel"])):
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
        record["protoc_argv_paths"] = paths
        if not paths:
            record["source_protoc_match"] = False
            continue
        assert len(paths) == 1, "Expected one actual protoc argv path"
        path = paths[0]
        file = input_file(definition["inputRootDigest"], path)
        matching = [(link, f) for link, _, _, _ in links for f in link["outputs"] if f["path"] == path and f["digest"] == file["digest"]]
        assert len(matching) <= 1
        record.update(protoc_path=path, protoc_digest=file["digest"], source_protoc_match=bool(matching) and "-exec-" in path)
        if record["source_protoc_match"]:
            record["cpp_link_action"] = matching[0][0]["action"]
    observed = any(r.get("source_protoc_match") for r in row["observations"])
    row["source_protoc_use"] = "OBSERVED_IN_INVOCATION" if observed else "UNVERIFIED_NO_MATCHING_EXECUTIONS"
    row["containerd_generated_action_count"] = sum("com_github_containerd_" in e["targetLabel"] for e in candidates)



def shipped_bindings(row, inventory):
    candidates = [e for e in inventory if e.get("actionMnemonic") == "GoCompilePkg"
                  and "com_github_containerd_containerd_api//runtime/task/v2:" in e.get("targetLabel", "")]
    row["matching_actions"] = len(candidates)
    assert candidates and len(candidates) <= 4
    record, definition, command, _ = action(candidates[0])
    row["action"] = record
    save()
    successful(record)
    args = command["arguments"][:]
    for arg in args[:]:
        if arg.startswith("-param="):
            file = input_file(definition["inputRootDigest"], arg[len("-param="):])
            args.extend(shlex.split(blob(file["digest"], INV).decode()))
    paths = sorted({a for a in args if a.startswith("external/") and a.endswith(".pb.go")})
    row["declared_shipped_bindings"] = []
    for path in paths:
        file = input_file(definition["inputRootDigest"], path)
        row["declared_shipped_bindings"].append({"path": path, "digest": file["digest"]})
        save()
    assert row["declared_shipped_bindings"]
    row["gvisor_consumer"] = "pkg/shim/v1/extension/BUILD -> @com_github_containerd_containerd_api//runtime/task/v2:go_default_library"


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


def retained(row):
    assert git("rev-parse", "HEAD^") == PARENT
    assert git("diff", "--name-only", PARENT, "HEAD") == ".github/workflows/build.yml\ntest/rbe/external_protoc_recover.py"
    data = (AREA / "original.zip").read_bytes()
    row["archive_sha256"] = sha256(data).hexdigest()
    assert row["archive_sha256"] == "bc8c9d79000171a0fbcd20ec6e44f0246536c68693e835f4ba4e474e6a7cc53b"
    with ZipFile(AREA / "original.zip") as archive:
        assert archive.namelist() == ["external-protoc-recovery.json"]
        previous = json.loads(archive.read("external-protoc-recovery.json"))
    assert previous["source"] == PRODUCT and previous["invocation"] == INV
    assert previous["verdict"] == "INCOMPLETE" and previous["original_client_exit"] == 33
    row.update(original_recovery=previous, reused_run=41414645893, reused_artifact=13126238909,
               repeated_builds=0, repeated_queries=0, repeated_binary_executions=0)
    for name in ("original", "last_log_chunk", "original_system_compiler", "original_client_diagnostic",
                 "upb_compile", "abseil_compile", "protoc_links", "shim_link", "protoc_cli"):
        assert previous["phases"][name]["status"] == "PASS"
        RESULT["phases"][name] = previous["phases"][name]
    return previous


def reconcile_shim(row, previous):
    original = previous["phases"]["shim_cli"]
    assert original["status"] == "INCOMPLETE" and original["collection_site"] == "external_protoc_recover.py:203"
    assert len(original["binaries"]) == 1
    binary = original["binaries"][0]
    assert binary["exit"] == 0 and binary["expected_output"] is False and binary["elf_machine"] == 62
    assert binary["action"] == {"hash": "48844ab5a157c7a892d79e5f4999282f534d591fc17b436dbd58f54bc8f3b013", "sizeBytes": "388"}
    assert binary["digest"] == {"hash": "b3c7828791319a92e3c4a95311c0728d9846c476b77e4aa610331ff074a763b1", "sizeBytes": "16679480"}
    # containerd v2.1.5 prints filepath.Base(os.Args[0]), not a fixed name.
    # Its unchanged defaults and gVisor's pinned SDK reproduce the captured bytes.
    expected = ("recovered-containerd-shim-runsc-v1-0:\n"
                "  Version:  2.1.5+unknown\n  Revision: \n  Go version: go1.27.3\n\n")
    assert "go 1.27.3\n" in git("show", PRODUCT + ":go.mod")
    assert "github.com/containerd/containerd/v2 v2.1.5" in git("show", PRODUCT + ":go.mod")
    assert sha256(expected.encode()).hexdigest() == binary["output_sha256"]
    row.update(original_record=original, exact_output=expected, output_sha256=binary["output_sha256"],
               containerd_source="fcd43222d6b07379a4be9786bda52438f0dd16a1",
               print_source="pkg/shim/shim.go:220-224", defaults_source="version/version.go",
               reconciliation="Exact byte hash; original process exited zero; no repeated startup")


def main():
    previous = phase("retained_receipt", retained)
    if previous is not None:
        phase("shim_cli", lambda row: reconcile_shim(row, previous))
        links = [(r, None, None, None) for r in previous["phases"]["protoc_links"]["actions"]]
        entries = phase("generation_inventory", lambda row: inventory(row, previous))
        if entries is not None:
            phase("source_protoc_consumers", lambda row: consumers(row, links, entries))
            phase("containerd_shipped_bindings", lambda row: shipped_bindings(row, entries))
    required = ("retained_receipt", "original", "upb_compile", "abseil_compile", "protoc_links", "shim_link", "protoc_cli", "shim_cli", "source_protoc_consumers")
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
