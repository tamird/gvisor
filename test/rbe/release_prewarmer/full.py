"""Verify the optimized installed fileset and the Make installation on the two declared remote workers."""

from collections import Counter
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import stat
import sys
import traceback
import uuid
import zipfile

import qualify as q

STAGE = "//:release"
LINKS = {
    "//runsc:runsc": "runsc",
    "//shim:containerd-shim-runsc-v1": "containerd-shim-runsc-v1",
    "//runsc/cmd/sentry:gvisor_sentry": "gvisor-bin/gvisor_sentry",
    "//runsc/checkpointgofer:checkpointgofer_binary": "gvisor-bin/checkpointgofer",
    "//runsc/cmd/netgofer:runsc-netgofer": "gvisor-bin/runsc-netgofer",
    "//runsc/cmd/metricserver:runsc-metric-server": "gvisor-bin/runsc-metric-server",
}
FILES = set(LINKS.values()) | {
    "gvisor-bin/gvisor-sentry-prewarmer", "gvisor-bin/runsc-fd-parking",
    "gvisor-bin/gvisor-proc-umounter",
}
SELECT = r"""
if .spawn then
  .spawn | . as $s |
  {kind:"action",runner,cache:(.cacheHit//false),exit:(.exitCode//0),status:((.status//"")|if .=="" then "" else "reported" end),platform:
    [.platform.properties[]? | select(.name|IN("Arch","OSFamily","container-image","recycle-runner","workload-isolation-type"))]} +
  (if (.targetLabel==$owner or (.mnemonic=="GoLink" and ($links|has($s.targetLabel))) or
       (.exitCode//0)!=0 or (.status//"")!="") then
    {label:.targetLabel,mnemonic,executable:.args[0],digest,outputs,
     temp:[.envVars[]? | select(.name|IN("TMP","TMPDIR","TEMP","GOTMPDIR")) | .name]}
   else {} end)
elif .file and (.file.path|test("/bin/(release/|runsc/.*_/|shim/.*_/)")) then
  {kind:"file",id,path:.file.path,digest:.file.digest}
else empty end
"""


def original_actions():
    """Reuse the fixed-origin reader for the three original failed links."""
    from cas_inputs import blob, rpc

    expected = {
        "amd64": ("d73fc334-3c96-422d-b21c-5315bce04ad0", {
            "//runsc/procumounter:_linked/gvisor-proc-umounter": "43ba669962f12f3fc2863cc708fe3303e1ceff6b4f118bfa55849bb932818e22",
            "//runsc/fdparking:_linked/runsc-fd-parking": "9b2b50ca28a9414efc3e3f30de505df9baaba08eae67ae7d38eb69cd5c04816b",
        }),
        "arm64": ("4b05d871-64c8-4d48-b2b9-0af756648846", {
            "//runsc/fdparking:_linked/runsc-fd-parking": "57b1afc919fee4fbac5b033988fe153c1ff7fa6f858a32b6cf23df8d4053a938",
        }),
    }
    observation = {"status": "INCOMPLETE", "actions": [], "started": q.now(),
                   "artifact": 13390432092, "run": 41971849807,
                   "original_source": "82a843a3470a1d779cc800208e720c1c3c97ca54"}
    q.save("original-actions", observation)
    try:
        raw = (q.TMP / "original.zip").read_bytes()
        assert len(raw) == 2154
        assert hashlib.sha256(raw).hexdigest() == "af10f97a43e33a53051ccf2952b00d8e3f0f9513fd00bc40119edae2981ddb97"
        observation["zip_sha256"] = hashlib.sha256(raw).hexdigest()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            assert set(archive.namelist()) == {"amd64.json", "arm64.json", "verdict.json"}
            assert all(item.file_size <= 256 * 1024 for item in archive.infolist())
            previous = {name: json.loads(archive.read(name + ".json")) for name in expected}
            verdict = json.loads(archive.read("verdict.json"))
        assert verdict["carrier"] == "31c55ee5ff5437645b06f1aeb21a07de7ef7823c"
        assert verdict["source"] == observation["original_source"] and verdict["clean_source"]
        assert not verdict["verified"] and verdict["failed"] == ["amd64", "arm64"] and not verdict["missing"]
        for arch, (invocation, failures) in expected.items():
            old = previous[arch]
            assert old["source"] == observation["original_source"] and old["status"] == "FAIL"
            assert old["native_exit"] == old["events"]["exit"] == 1
            assert old["events"]["invocation"] == old["requested_invocation"] == invocation
            assert old["events"]["owners"] == [False] and old["events"]["test_results"] == 0
            assert len(old["actions"]) == len(failures)
            assert {a["label"]: a["digest"]["hash"] for a in old["actions"]} == failures
            for witness in old["actions"]:
                digest = witness["digest"]
                assert int(digest["sizeBytes"]) == 376 and witness["exit"] == 1
                assert witness["runner"] == "remote" and not witness["cache"] and witness["worker_matches"]
                assert witness["mnemonic"] == "CppLink"
                item = {"arch": arch, "invocation": invocation, "owner": witness["label"],
                        "action_digest": digest, "status": "INCOMPLETE", "streams": {}}
                observation["actions"].append(item)
                q.save("original-actions", observation)
                try:
                    matches = rpc("GetExecution", {
                        "executionLookup": {"invocationId": invocation, "actionDigestHash": digest["hash"]},
                        "inlineExecuteResponse": True,
                    }).get("execution", [])
                    action, = [a for a in matches if a.get("actionDigest", {}).get("hash") == digest["hash"]
                               and str(a["actionDigest"]["sizeBytes"]) == str(digest["sizeBytes"])]
                    response = action["executeResponse"]
                    result = response["result"]
                    assert action["stage"] in (4, "COMPLETED")
                    assert result.get("exitCode", 0) == 1 and not response.get("cachedResult")
                    item.update(exit=1, response_status_code=int(response.get("status", {}).get("code", 0)))
                    for stream in ("stdout", "stderr"):
                        advertised = result.get(stream + "Digest", {})
                        if advertised:
                            assert re.fullmatch(r"[0-9a-f]{64}", advertised["hash"])
                            assert 0 <= int(advertised["sizeBytes"]) <= 65536
                        data = (blob(advertised, invocation) if int(advertised.get("sizeBytes", 0)) else
                                base64.b64decode(result.get(stream + "Raw", ""), validate=True))
                        assert len(data) <= 65536
                        sha = hashlib.sha256(data).hexdigest()
                        if advertised:
                            assert len(data) == int(advertised["sizeBytes"]) and sha == advertised["hash"]
                        text = data.decode(errors="replace").lower()
                        item["streams"][stream] = {
                            "sha256": sha, "bytes": len(data), "advertised_digest": advertised,
                            "missing_libraries": [name for name in ("pthread", "dl")
                                                  if re.search(r"unable to find library\s+-l" + name + r"\b", text)],
                            "categories": [name for name, pattern in (
                                ("missing_input_or_interpreter", r"no such file or directory|enoent|unable to open"),
                                ("undefined_symbol", r"undefined symbol|undefined reference"),
                                ("relocation", r"relocation"),
                                ("permission_denied", r"permission denied|eacces"),
                                ("exec_format", r"exec format|enoexec"),
                                ("missing_library", r"unable to find library"),
                            ) if re.search(pattern, text)],
                        }
                    item["status"] = "ORIGINAL_ACTION_OUTPUT_READ"
                except Exception as error:
                    item["error"] = type(error).__name__
                q.save("original-actions", observation)
        if len(observation["actions"]) == 3 and all(
                item["status"] == "ORIGINAL_ACTION_OUTPUT_READ" for item in observation["actions"]):
            observation["status"] = "ORIGINAL_ACTION_OUTPUT_READ"
    except Exception as error:
        observation["error"] = type(error).__name__
    observation["finished"] = q.now()
    q.save("original-actions", observation)
    # Metadata availability cannot replace either original failure or hide the
    # independently requested corrected-source build and installation result.
    return 0


def actions(path, arch):
    expected = {"Arch": arch, "OSFamily": "linux", "workload-isolation-type": "oci",
                "recycle-runner": "false", "container-image": "docker://gcr.io/distroless/static-debian13@" + q.PIN[arch]}
    counts = Counter()
    files, selected = {}, []
    with subprocess.Popen([str(q.TMP / "bb"), "print", "--raw", "--compact_execution_log=" + str(path)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as reader:
        with subprocess.Popen(["jq", "-c", "--arg", "owner", STAGE, "--argjson", "links",
                               json.dumps(LINKS), SELECT], stdin=reader.stdout, stdout=subprocess.PIPE,
                              text=True) as rows:
            reader.stdout.close()
            for line in rows.stdout:
                item = json.loads(line)
                if item["kind"] == "file":
                    files[item["id"]] = item
                    continue
                props = {p["name"]: p["value"] for p in item.pop("platform")}
                runner = item["runner"] or "unknown"
                counts[runner] += 1
                if props != expected:
                    counts["different_platform"] += 1
                if item.get("label"):
                    item["worker_matches"] = props == expected
                    selected.append(item)
            assert rows.wait() == 0
        assert reader.wait() == 0
    for item in selected:
        item["outputs"] = [files[o["outputId"]] for o in (item.pop("outputs", None) or [])
                           if o.get("outputId") in files]
    return dict(counts), selected


def compiler_result(arch):
    bep, compact, log = [q.TMP / (arch + x) for x in (".bep", ".compact", ".log")]
    row = {"arch": arch, "source": q.PRODUCT, "started": q.now(), "status": "INCOMPLETE",
           "requested_invocation": str(uuid.uuid4())}
    q.save(arch, row)
    exe = "//test/rbe/release_prewarmer:" + arch
    config = ["--config=" + {"amd64": "x86_64", "arm64": "aarch64"}[arch], "-c", "opt",
              "--extra_execution_platforms=" + exe, "--host_platform=" + exe,
              "--extra_toolchains=//test/rbe/go_bootstrap:linux_" + arch + "_ash",
              "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
              "--extra_toolchains=//test/rbe/protoc_check:static_python" + ("_arm64" if arch == "arm64" else ""),
              "--remote_accept_cached", "--remote_download_outputs=toplevel", "--jobs=32",
              "--lockfile_mode=off", "--color=no", "--curses=no"]
    try:
        with log.open("w") as output:
            run = subprocess.run(["timeout", "--signal=INT", "--kill-after=30", "4500",
                                  "bazel", *q.PROOF, "build", *config,
                                  "--invocation_id=" + row["requested_invocation"],
                                  "--build_event_json_file=" + str(bep), "--execution_log_compact_file=" + str(compact),
                                  STAGE], stdout=output, stderr=subprocess.STDOUT)
        row.update(native_exit=run.returncode, returned=q.now())
        if run.returncode:
            row["status"] = "INCOMPLETE" if run.returncode in (124, 137, -9, -15) else "FAIL"
        q.save(arch, row)
        row["events"] = q.events(bep)
        row["diagnostics"] = q.diagnostics(log) if run.returncode else []
        q.save(arch, row)
        if compact.is_file():
            row["runners"], row["actions"] = actions(compact, arch)
        q.save(arch, row)
        if run.returncode:
            return row
        assert row["events"]["invocation"] == row["requested_invocation"]
        assert row["events"]["test_results"] == 0
        assert row["events"]["owners"] == [True] and row["events"]["exit"] == 0 and row["events"]["bazel"] == "9.3.1"
        root = Path("bazel-bin/release")
        targets = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
        assert targets == FILES
        row["binaries"] = binaries = {name: q.elf(root / name) for name in sorted(FILES)}
        q.save(arch, row)
        assert all(b["machine"] == {"amd64": 62, "arm64": 183}[arch] and b["load"] and b["entry"]
                   and os.access(root / name, os.X_OK) for name, b in binaries.items())
        prewarmer = binaries["gvisor-bin/gvisor-sentry-prewarmer"]
        assert 0 < prewarmer["bytes"] <= 4096 and not prewarmer["interp"] and not prewarmer["dynamic"]
        matched, linked = set(), set()
        for a in row["actions"]:
            assert a["worker_matches"] and not a["status"] and not a["exit"]
            assert a["runner"] == "remote" and not a["cache"]
            if a["label"] == STAGE:
                assert a["runner"] == "remote" and not a["cache"]
                assert a["mnemonic"] == "CopyFile" and a["executable"].startswith("external/bazel_lib") and a["executable"].endswith("/coreutils")
                o, = a["outputs"]
                name = o["path"].split("/bin/release/")[1]
                assert name not in matched
                matched.add(name)
            else:
                assert a["mnemonic"] == "GoLink" and a["executable"].endswith("/builder")
                name = LINKS[a["label"]]
                assert name not in linked
                linked.add(name)
                matching = [o for o in a["outputs"] if o["digest"]["hash"] == binaries[name]["sha256"]]
                o, = matching
            assert o["digest"]["hash"] == binaries[name]["sha256"] and int(o["digest"]["sizeBytes"]) == binaries[name]["bytes"]
        assert matched == FILES and linked == set(LINKS.values())
        assert not row["runners"].get("different_platform")
        assert set(row["runners"]) <= {"remote", "remote cache hit"}, row["runners"]
        installed = q.TMP / (arch + "-installation")
        installed.mkdir()
        with log.open("a") as output:
            copied = subprocess.run(["make", "copy", "DOCKER_BUILD=false",
                                     "STARTUP_OPTIONS=" + " ".join(q.PROOF),
                                     "BAZEL_OPTIONS=" + " ".join(config),
                                     "TARGETS=" + STAGE, "DESTINATION=" + str(installed)],
                                    stdout=output, stderr=subprocess.STDOUT, timeout=600)
        row.update(make_exit=copied.returncode, execution_finished_at=q.now())
        if copied.returncode:
            row.update(status="FAIL", diagnostics=q.diagnostics(log))
            return row
        installed_files = {str(p.relative_to(installed)) for p in installed.rglob("*") if p.is_file()}
        row["installed_files"] = sorted(installed_files)
        assert installed_files == FILES
        for name in installed_files:
            mode = (installed / name).lstat().st_mode
            assert stat.S_ISREG(mode) and mode & 0o111 == 0o111
            assert q.elf(installed / name) == binaries[name]
        row["status"] = "PASS"
    except (AssertionError, KeyError, ValueError, OSError, subprocess.SubprocessError) as err:
        row.update(collection_error=type(err).__name__, line=traceback.extract_tb(err.__traceback__)[-1].lineno)
    finally:
        row.setdefault("execution_finished_at", row.get("returned"))
        row["verified_at"] = q.now()
        q.save(arch, row)
    return row


def main():
    arch = sys.argv[1]
    if arch == "original":
        return original_actions()
    assert not any(os.environ.get(name) for name in ("BUILDBUDDY_API_KEY", "ACCESS", "GH_TOKEN", "GITHUB_TOKEN"))
    if arch == "verdict":
        outcomes = {}
        for name in q.PIN:
            path = q.OUT / (name + ".json")
            if path.is_file():
                outcomes[name] = json.loads(path.read_text())
        executed = sorted(k for k, row in outcomes.items() if "native_exit" in row)
        failed = sorted(k for k, row in outcomes.items() if row["status"] != "PASS")
        missing = sorted(set(q.PIN) - set(executed))
        clean = q.git("rev-parse", "HEAD") == q.PRODUCT and not q.git("diff", "--name-only", "HEAD")
        good = clean and not failed and not missing
        done = [row["execution_finished_at"] for row in outcomes.values() if row.get("execution_finished_at")]
        original = json.loads((q.OUT / "original-actions.json").read_text())
        q.save("verdict", {"source": q.PRODUCT, "carrier": os.environ["GITHUB_SHA"], "verified": good,
                           "clean_source": clean, "expected": sorted(q.PIN), "executed": executed, "failed": failed,
                           "missing": missing, "execution_finished_at": max(done, default=None), "verified_at": q.now(),
                           "original_native_control": {"source": "1668d83dcd713c54dacd019869318254122cb283", "run": 41970371311, "artifact": 13389881117, "scope": "13 shell launches failed; no paired RBE result"},
                           "original_remote_control": {"run": 41971849807, "artifact": 13390432092,
                                                       "status": "FAIL", "action_recovery": original["status"]},
                           "prior_nine_file_run": 41765228449, "prior_nine_file_artifact": 13292873920,
                           "scope": "Paired remote build and coordinator Make installation; no guest or test execution"})
        return int(not good)
    assert arch in q.PIN and q.git("rev-parse", "HEAD") == q.PRODUCT and not q.git("diff", "--name-only", "HEAD")
    return int(compiler_result(arch)["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
