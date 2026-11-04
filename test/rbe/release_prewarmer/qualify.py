"""Bind each native release sidecar to its ordinary LLVM compile and link."""

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import traceback

from image_inventory import PIN

TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "release-evidence"
PRODUCT = "2882b32e315991b4063c0baf2aedd40539ce6fd4"
PUBLIC = "//runsc/prewarmer:prewarmer_binary"
LINKED = "//runsc/prewarmer:_linked/gvisor-sentry-prewarmer"
STAGED = "runsc/prewarmer/gvisor-sentry-prewarmer"
PROOF = ["--bazelrc=" + str(TMP / "sidecar.bazelrc")]
PROJECTION = r"""
if .spawn and (.spawn.targetLabel == $owner or (.spawn.exitCode // 0) != 0 or (.spawn.status // "") != "") then
  .spawn | {kind:"action",label:.targetLabel,mnemonic,runner,cache:(.cacheHit//false),
     exit:(.exitCode//0),status,executable:.args[0],digest,outputs,
     temp:[.envVars[]? | select(.name|IN("TMP","TMPDIR","TEMP","GOTMPDIR")) | .name],
     platform:[.platform.properties[]? | select(.name|IN("Arch","OSFamily","container-image","recycle-runner","workload-isolation-type"))]}
elif .file and (.file.path | test("/runsc/prewarmer/.*(gvisor-sentry-prewarmer|prewarmer(\\.pic)?\\.o)$")) then
  {kind:"file",id,path:.file.path,digest:.file.digest}
else empty end
"""


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def save(name, row):
    (OUT / (name + ".json")).write_text(json.dumps(row, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def collect_actions(compact):
    with subprocess.Popen([str(TMP / "bb"), "print", "--raw", "--compact_execution_log=" + str(compact)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as read:
        result = subprocess.run(["jq", "-c", "--arg", "owner", LINKED, PROJECTION],
                                stdin=read.stdout, capture_output=True, text=True, check=True)
        read.stdout.close()
        assert read.wait() == 0
    items = [json.loads(line) for line in result.stdout.splitlines()]
    files = {r["id"]: r for r in items if r["kind"] == "file"}
    result = [r for r in items if r["kind"] == "action"]
    for row in result:
        row["outputs"] = [files[o["outputId"]] for o in (row.pop("outputs", None) or []) if o.get("outputId") in files]
        row["platform"] = {prop["name"]: prop["value"] for prop in row.get("platform", [])}
    return result


def events(path):
    receipt = {"owners": []}
    if path.exists():
        for line in path.open():
            event = json.loads(line)
            if "started" in event:
                receipt.update(invocation=event["started"]["uuid"], bazel=event["started"].get("buildToolVersion"))
            if "finished" in event:
                receipt.update(finished=event["finished"]["finishTime"], exit=event["finished"]["exitCode"].get("code", 0))
            target = event["id"].get("targetCompleted", {})
            if target.get("label") == PUBLIC and not target.get("aspect"):
                receipt["owners"].append(event.get("completed", {}).get("success", False))
    return receipt


def diagnostics(path):
    rows = []
    pattern = re.compile(r"(?P<location>(?:[a-zA-Z0-9_+.-]+/)*[a-zA-Z0-9_+.-]+\.(?:go|bzl|cc|s|S|c|h):[0-9]+(?::[0-9]+)?)")
    reasons = ("undefined:", "not found", "undeclared", "no such file", "permission denied",
               "no matching toolchains", "does not have mandatory providers", "no such target",
               "no such package", "compilation failed", "build did not complete", "error:",
               "not defined", "missing strict dependencies", "has no field or method", "timeout",
               "unauthenticated", "resource_exhausted", "failed to fetch", "command not found")
    for line in path.read_text(errors="replace").splitlines():
        kinds = [word for word in reasons if word in line.lower()]
        if not kinds:
            continue
        item = {"kinds": kinds, "locations": pattern.findall(line)[:4]}
        if item not in rows:
            rows.append(item)
    return rows[:18]


def elf(path):
    raw = path.read_bytes()
    assert raw[:6] == b"\x7fELF\x02\x01" and len(raw) > 64
    base = struct.unpack_from("<Q", raw, 32)[0]
    size, count = struct.unpack_from("<HH", raw, 54)
    types = [struct.unpack_from("<I", raw, base + i * size)[0] for i in range(count)]
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
            "machine": struct.unpack_from("<H", raw, 18)[0], "entry": struct.unpack_from("<Q", raw, 24)[0],
            "load": 1 in types, "interp": 3 in types, "dynamic": 2 in types}


def build(arch):
    row = {"source": PRODUCT, "owner": PUBLIC, "status": "INCOMPLETE", "start": now()}
    bep, compact, log = [TMP / (arch + suffix) for suffix in (".bep", ".compact", ".log")]
    platform = "//test/rbe/release_prewarmer:" + arch
    opts = ["--config=" + {"amd64": "x86_64", "arm64": "aarch64"}[arch], "-c", "opt",
            "--extra_execution_platforms=" + platform, "--host_platform=" + platform,
            "--extra_toolchains=//test/rbe/go_bootstrap:linux_" + arch + "_ash",
            "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
            "--remote_accept_cached", "--remote_download_outputs=toplevel", "--jobs=64", "--lockfile_mode=off",
            "--build_event_json_file=" + str(bep), "--execution_log_compact_file=" + str(compact),
            "--color=no", "--curses=no"]
    try:
        with log.open("w") as output:
            run = subprocess.run(["bazel", *PROOF, "build", *opts, PUBLIC], stdout=output, stderr=subprocess.STDOUT)
        row.update(native_exit=run.returncode, returned=now())
        if run.returncode:
            row["status"] = "FAIL"
        save(arch, row)
        row["events"] = events(bep)
        row["diagnostics"] = diagnostics(log)
        save(arch, row)
        if compact.exists():
            row["actions"] = collect_actions(compact)
        save(arch, row)
        if run.returncode:
            return row
        row["binary"] = binary = elf(Path("bazel-bin") / STAGED)
        compiled, linked = [[a for a in row["actions"] if a["label"] == LINKED and a["mnemonic"] == m]
                            for m in ("CppCompile", "CppLink")]
        assert len(compiled) == len(linked) == 1
        expected_platform = {"Arch": arch, "OSFamily": "linux", "workload-isolation-type": "oci",
                             "recycle-runner": "false", "container-image": "docker://gcr.io/distroless/static-debian13@" + PIN[arch]}
        for a, program in [(compiled[0], "clang"), (linked[0], "clang++")]:
            assert a["runner"] == "remote" and not a["cache"] and not a["status"] and a["exit"] == 0
            assert a["platform"] == expected_platform and not a["temp"]
            assert a["executable"].startswith("external/llvm+") and a["executable"].endswith("/bin/" + program)
            assert a["digest"]["hash"] and a["outputs"]
        assert any(o["digest"]["hash"] == binary["sha256"] and int(o["digest"]["sizeBytes"]) == binary["bytes"]
                   for o in linked[0]["outputs"])
        assert binary["machine"] == {"amd64": 62, "arm64": 183}[arch] and binary["entry"] and binary["load"]
        assert not binary["interp"] and not binary["dynamic"] and 0 < binary["bytes"] <= 4096
        assert row["events"]["exit"] == 0 and row["events"]["bazel"] == "9.3.1" and row["events"]["owners"] == [True]
        row["status"] = "PASS"
    except (ValueError, KeyError, AssertionError, OSError, subprocess.SubprocessError) as err:
        row.update(collection_error=type(err).__name__, line=traceback.extract_tb(err.__traceback__)[-1].lineno)
    finally:
        row["verified_at"] = now()
        save(arch, row)
    return row


def main():
    OUT.mkdir(exist_ok=True)
    assert git("rev-parse", "HEAD^") == PRODUCT
    assert all(f.startswith("test/rbe/release_prewarmer/") or f == ".github/workflows/build.yml"
               for f in git("diff", "--name-only", PRODUCT, "HEAD").splitlines())
    assert Path(".bazelversion").read_text().strip() == "9.3.1"
    query = subprocess.run(["bazel", *PROOF, "query", "--lockfile_mode=off",
                            "deps(//:release, 1) intersect " + PUBLIC, "--output=label"],
                           stdout=subprocess.PIPE, stderr=(TMP / "query.log").open("w"), text=True)
    record = {"source": PRODUCT, "carrier": git("rev-parse","HEAD"), "query_exit": query.returncode,
              "direct_release_member": query.stdout.splitlines() == [PUBLIC], "query_diagnostics": diagnostics(TMP / "query.log")}
    save("verdict",record)
    for arch in PIN:
        record[arch] = build(arch)["status"]
    record["clean_source"] = not git("diff", "--name-only", "HEAD")
    record["finished"] = now()
    record["verified"] = bool(record["direct_release_member"] and not query.returncode and record["clean_source"]
                              and all(record[arch] == "PASS" and (OUT / (arch + "-image.json")).is_file() for arch in PIN))
    save("verdict",record)
    return 0 if record["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
