"""Build one runsc dependency and observe its actual Go actions on empty Linux."""

import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile

TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "go-temp-evidence"
STOCK = "851d04fc2918adcaa60a78fd9fa7e766bc4fec68"
FIXED = "0b29a15b69911c1522cf3e10f2458aa85d21ef4c"
PACKAGE = "//pkg/gohacks:gohacks"
METADATA = "//test/rbe/go_stdlib_workdir:metadata"
ROOT = "//test/rbe/empty_worker:root"
TARGETS = [PACKAGE, METADATA, ROOT]
IMAGE = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
PLATFORM = "//test/rbe/empty_worker:arm64_musl"
FLAGS = ["--config=aarch64", "--host_platform=" + PLATFORM, "--extra_execution_platforms=" + PLATFORM,
         "--extra_toolchains=//test/rbe/go_bootstrap:linux_arm64_ash",
         "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain", "--remote_accept_cached",
         "--remote_download_outputs=toplevel", "--noremote_local_fallback", "--jobs=32",
         "--keep_going", "--color=no", "--curses=no"]
OUTPUT_NAMES = {"runtime.a", "stdlib.pkg.json", "gohacks.a", "gohacks.x", "root.txt"}
EXPECTED = {"GoStdlib": "runtime.a", "GoStdlibList": "stdlib.pkg.json", "GoCompilePkg": "gohacks.a"}
FILTER = r'''
if .spawn != null and (.spawn.mnemonic | IN("GoStdlib","GoStdlibList","GoCompilePkg","ObserveWorkerRoot")) then
  .spawn | {kind:"spawn", label:.targetLabel, mnemonic, runner, cache:(.cacheHit//false), status,
    exit:(.exitCode//0), argv0:.args[0], digest,
    properties:[.platform.properties[]? | select(.name | IN("Arch","OSFamily","container-image","workload-isolation-type"))],
    env:[.envVars[]? | select(.name | IN("GOOS","GOARCH","CGO_ENABLED","TMPDIR","TMP","TEMP","GOTMPDIR"))], outputs}
elif .file != null and (.file.path | test("/(stdlib\\.pkg\\.json|gohacks\\.(a|x)|root\\.txt)$")) then
  {kind:"files", id, files:[.file]}
elif .directory != null and (.directory.path | endswith("/pkg")) then
  {kind:"files", id, archives:([.directory.files[]? | select(.path | endswith(".a"))]|length),
   files:[.directory as $d | $d.files[]? | select(.path|endswith("/runtime.a")) | .path=($d.path+"/"+.path)]}
else empty end
'''

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def save(name, data):
    (OUT / name).write_text(json.dumps(data, indent=2) + "\n")

def original_events(bep):
    summary = {"targets": {}}
    if not bep.is_file():
        return summary
    for line in bep.open():
        event = json.loads(line)
        id_ = event["id"]
        if "started" in event:
            summary["invocation"] = event["started"]["uuid"]
        if "finished" in event:
            summary["finish"] = event["finished"]["finishTime"]
            summary["exit_code"] = event["finished"]["exitCode"].get("code", 0)
        target = id_.get("targetCompleted", {})
        if target.get("label") in TARGETS and not target.get("aspect"):
            summary["targets"][target["label"]] = event.get("completed", {}).get("success", False)
    return summary

def actions(compact):
    spawns, blobs = [], {}
    with subprocess.Popen([str(TMP / "bb"), "print", "--raw", "--compact_execution_log=" + str(compact)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as reader:
        query = subprocess.run(["jq", "-c", FILTER], stdin=reader.stdout, stdout=subprocess.PIPE, check=True)
        reader.stdout.close()
        if reader.wait():
            raise ValueError("Compact log reader failed")
    for line in query.stdout.splitlines():
        item = json.loads(line)
        if item["kind"] == "files":
            blobs[item["id"]] = item
        else:
            spawns.append(item)
    for item in spawns:
        paths = [blobs[id_["outputId"]] for id_ in item.pop("outputs") if id_.get("outputId") in blobs]
        item["files"] = [file for group in paths for file in group["files"] if Path(file["path"]).name in OUTPUT_NAMES]
        item["archives"] = sum(group.get("archives", 0) for group in paths)
        item["properties"] = {k["name"]: k["value"] for k in item["properties"]}
        item["env"] = {k["name"]: k["value"] for k in item["env"]}
    return spawns

def native(row):
    props = row["properties"]
    return (props.get("Arch") == "arm64" and props.get("OSFamily") == "linux"
            and props.get("container-image") == IMAGE and props.get("workload-isolation-type") == "oci")

def verify(row, prior=None):
    selected = [a for a in row["actions"] if (a["label"].endswith("//:stdlib") or a["label"] in [PACKAGE, ROOT])]
    expected = [a for a in selected if a["mnemonic"] in EXPECTED]
    no_environment = all(native(a) and a["env"].get("GOOS") == "linux" and a["env"].get("GOARCH") == "arm64"
                         and not set(a["env"]).intersection(("TMP", "TEMP", "TMPDIR", "GOTMPDIR"))
                         for a in expected)
    needed = {"GoStdlib", "GoStdlibList"}
    if row["phase"] == "stock" and row["native_exit"] != 0:
        row["observed"] = "failure creating a Go temporary directory"
        row["verified"] = (row["native_exit"] != 0 and row["events"].get("exit_code", 0) != 0 and no_environment
            and any(re.search("/tmp.*no such file or directory", line, re.I) for line in row["diagnostics"])
            and any(a["mnemonic"] in needed and a["exit"] and a["runner"] == "remote" and not a["cache"] for a in expected))
    else:
        row["observed"] = "completed with no global temporary-directory setting"
        matched = {a["mnemonic"] for a in expected if a["runner"] == "remote" and not a["cache"] and not a["exit"]
                   and any(Path(f["path"]).name == EXPECTED[a["mnemonic"]] and f["digest"].get("hash")
                           and int(f["digest"].get("sizeBytes", 0)) > 0 for f in a["files"])}
        root_lines = row["root"]
        probe_bytes = ("\n".join(root_lines) + "\n").encode()
        root_sha = hashlib.sha256(probe_bytes).hexdigest()
        root_matches = False
        for observation in [row, prior]:
            if not observation or observation["version"] != row["version"] or not observation["events"]["targets"].get(ROOT):
                continue
            for a in observation["actions"]:
                if a["label"] != ROOT or a["mnemonic"] != "ObserveWorkerRoot" or not native(a) or a["exit"]:
                    continue
                if any(f["digest"]["hash"] == root_sha and int(f["digest"]["sizeBytes"]) == len(probe_bytes)
                       for f in a["files"] if Path(f["path"]).name == "root.txt"):
                    row["root_origin"] = {"invocation": observation["events"]["invocation"], "action": a["digest"],
                                          "sha256": root_sha, "bytes": len(probe_bytes)}
                    root_matches = True
                    break
            if root_matches:
                break
        row["verified"] = (row["native_exit"] == 0 and row["events"].get("exit_code") == 0 and no_environment
            and all(row["events"]["targets"].get(t) for t in TARGETS) and set(EXPECTED) == matched
            and root_matches and "machine aarch64" in root_lines and "root /hello" in root_lines
            and "root /tmp" not in root_lines and "exec /bin/sh no 127" in root_lines)

def run(phase, source, version, prior=None):
    subprocess.run(["git", "checkout", "-q", "--detach", source], check=True)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == source
    fixture = Path("test/rbe/go_stdlib_workdir")
    fixture.mkdir(exist_ok=True)
    for name in ["BUILD", "stdlib.bzl"]:
        (fixture / name).write_bytes((TMP / ("go-temp-" + name)).read_bytes())
    subprocess.run(["git", "diff", "--exit-code", "HEAD"], check=True)
    stem = version + "-" + phase
    bep, compact, log = [TMP / (stem + ext) for ext in (".bep", ".compact", ".log")]
    tool = ["bazel", "--bazelrc=" + str(TMP / "sidecar.bazelrc")]
    row = {"phase": phase, "source": source, "version": version, "started": now(), "verified": False}
    env = dict(os.environ, USE_BAZEL_VERSION=version)
    with log.open("w") as stream:
        row["native_exit"] = subprocess.run(["timeout", "--signal=INT", "--kill-after=20s", "13m", *tool, "build",
            *FLAGS, "--build_event_json_file=" + str(bep), "--execution_log_compact_file=" + str(compact),
            *TARGETS], env=env, stdout=stream, stderr=subprocess.STDOUT).returncode
    row["build_finished"] = now()
    save(stem + ".json", row)
    diag = []
    for line in log.read_text(errors="replace").splitlines():
        if re.search(r"api.?key|token|password|header=|authoriz|credential", line, re.I):
            continue
        match = re.search(r"(?:stat|mkdir|open) /tmp(?:/[^ :]+)?: no such file or directory", line, re.I)
        if match:
            diag.append(match.group(0))
        elif line.startswith(("ERROR:", "FATAL:")):
            if "not defined" in line or "cannot load" in line:
                diag.append("Bazel could not load a required definition")
            elif "patch" in line.lower():
                diag.append("Bazel reported a patch failure")
    row["diagnostics"] = diag[:16]
    try:
        row["events"] = original_events(bep)
        row["actions"] = actions(compact)
        if row["native_exit"] == 0:
            row["root"] = Path("bazel-bin/test/rbe/empty_worker/root.txt").read_text().splitlines()
        verify(row, prior)
    except (KeyError, OSError, ValueError, subprocess.SubprocessError) as err:
        row["collection_error"] = type(err).__name__
    row["source_after"] = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    row["tracked_clean"] = subprocess.run(["git", "diff", "--quiet", "HEAD"]).returncode == 0
    row["verified"] = bool(row["verified"] and row["source_after"] == source and row["tracked_clean"])
    row["verified_at"] = now()
    save(stem + ".json", row)
    print(stem, row["native_exit"], "verified", row["verified"], row.get("diagnostics", [])[:2], flush=True)
    return row

def main():
    OUT.mkdir(exist_ok=True)
    for version in ["9.3.1", "8.8.1"]:
        stock = run("stock", STOCK, version)
        if stock["native_exit"] == 0 and stock["verified"]:
            save("verdict.json", {"verified": True, "bazel": version, "source": STOCK,
                                  "disposition": "existing source already works without a global temporary-directory setting", "at": now()})
            return 0
        if not stock["verified"] and version == "9.3.1" and not any(
            a["mnemonic"] in EXPECTED for a in stock.get("actions", [])
        ):
            # Preserve a release-specific analysis failure; exercise the original
            # 8.8.1 setup only if 9.3.1 did not reach a qualifying Go action.
            continue
        fixed = run("fixed", FIXED, version, stock)
        verified = stock["verified"] and fixed["verified"]
        save("verdict.json", {"verified": verified, "bazel": version, "stock": stock["source"], "fixed": fixed["source"], "at": now()})
        return 0 if verified else 1
    return 1

def reconcile(archive, sha256):
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == sha256
    with zipfile.ZipFile(archive) as z:
        assert set(z.namelist()) == {"9.3.1-stock.json", "9.3.1-fixed.json", "verdict.json"}
        stock = json.loads(z.read("9.3.1-stock.json"))
        fixed = json.loads(z.read("9.3.1-fixed.json"))
        original_verdict = json.loads(z.read("verdict.json"))
    for row, source, name, prior in [(stock, STOCK, "stock", None), (fixed, FIXED, "fixed", stock)]:
        assert row["source"] == row["source_after"] == source and row["tracked_clean"]
        assert row["phase"] == name and row["version"] == "9.3.1"
        verify(row, prior)
    return {"verified": stock["verified"] and fixed["verified"], "archive_sha256": sha256,
            "original_verdict": original_verdict, "stock": stock, "fixed": fixed, "reconciled_at": now()}

if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--original":
        result = reconcile(Path(sys.argv[2]), sys.argv[3])
        print(json.dumps(result, indent=2))
        sys.exit(0 if result["verified"] else 1)
    sys.exit(main())
