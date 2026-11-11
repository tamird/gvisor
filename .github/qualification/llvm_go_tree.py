"""Run the real branch exporter on the native Linux client and inspect its local Git result."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile

import public_assets

work = Path(os.environ["RUNNER_TEMP"]) / "go-export-llvm"
out = work / "evidence/result.json"
src = os.environ["PRODUCT"]
previous = os.environ["LAST_GO"]
result = {"product": src, "compiler": "8178e3fe5fa3684427a9c78a66c0a268668de86c",
          "exporter": "45137f922cf026cfa764ca3177d624d1b49b5581",
          "previousGo": previous, "verified": False, "attempts": [], "publicInputs": {}}


def save():
    out.write_text(json.dumps(result, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args])


def sha(data):
    return hashlib.sha256(data).hexdigest()


def run_script():
    opts = work / "options.rc"
    opts.write_text("\n".join([
        "common --module_mirrors=https://bcr.cloudflaremirrors.com",
        "common --lockfile_mode=off", "build --jobs=4",
        "build --spawn_strategy=processwrapper-sandbox,local", "build --strategy=GoStdlib=processwrapper-sandbox",
        "build --strategy=GoStdlibList=processwrapper-sandbox", "build --remote_cache=",
        "build --remote_executor=", "build --disk_cache=", "build --bes_backend=",
        "build --build_event_publish_all_actions", f"build --build_event_json_file={work}/events.jsonl",
        f"common --distdir={public_assets.DIST}",
    ]) + "\n")
    for attempt in range(20):
        events = work / "events.jsonl"
        events.unlink(missing_ok=True)
        with (work / "script.log").open("wb") as log:
            proc = subprocess.run(["tools/go_branch.sh"], stdout=log, stderr=subprocess.STDOUT)
        text = (work / "script.log").read_text(errors="replace")
        errors = [line[:550] for line in text.splitlines() if line.startswith(("ERROR:", "fatal:", "rsync:", "unzip:"))]
        row = {"attempt": attempt+1, "scriptExit": proc.returncode, "logSHA256": sha(text.encode()), "errors": errors[-8:]}
        result["attempts"].append(row)
        save()
        if not proc.returncode:
            return
        urls = sorted({m.group() for line in text.splitlines() if "Error downloading" in line or "Download from " in line
                       for m in public_assets.URL.finditer(line)})
        added = False
        for url in urls:
            records = result["publicInputs"]
            if url in records or len(records) >= 38:
                continue
            budget = (1200 << 20) - sum(r.get("bytes", 0) for r in records.values())
            try:
                path, record = public_assets.release_distfile(url, public_assets.DIST, remaining_bytes=budget)
            except (OSError, ValueError, AssertionError, KeyError):
                continue
            with opts.open("a") as stream:
                stream.write(f"common --distdir={path}\n")
            records[url] = {k: record[k] for k in ("sha256", "bytes", "acquired") if k in record}
            added = True
        save()
        if not added:
            raise RuntimeError(f"export script exited {proc.returncode}: {errors[-3:]}")
    raise RuntimeError("public input recovery exhausted")


def check_archive():
    actual = git("rev-parse", "go").decode().strip()
    parents = git("show", "-s", "--format=%P", "go").decode().split()
    result.update(exportedGo=actual, gitParents=parents)
    assert parents == [previous, src] and git("rev-parse", "HEAD").decode().strip() == src
    files = {}
    for record in git("ls-tree", "-rz", "go").split(b"\0"):
        if not record:
            continue
        desc, name = record.split(b"\t", 1)
        mode, kind, oid = desc.decode().split()
        assert mode == "100644" and kind == "blob", (mode, kind, name)
        files[name.decode()] = oid
    wanted = {f"runsc/{owner}/{owner}.c" for owner in ("fdparking", "prewarmer", "procumounter")}
    wanted.update(("runsc/cmd/netgofer/main.go", "runsc/gofer/net/netgofer.go"))
    generated = set()
    for arch in ("amd64", "arm64"):
        generated.update((f"pkg/sentry/loader/vdsodata/vdso_{arch}.so",
                          f"pkg/sentry/platform/systrap/sysmsg/sighandler.built-in.{arch}.bin",
                          f"pkg/sentry/platform/systrap/sysmsg/sighandler_{arch}.go"))
    result["selected"] = {}
    archive = Path("bazel-bin/go_export.zip")
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        result.update(archiveBytes=archive.stat().st_size, archiveSHA256=sha(archive.read_bytes()),
                      members=len(names), gitEntries=len(files), tree=git("rev-parse", "go^{tree}").decode().strip())
        assert set(names) == set(files) and sorted(set(names)) == names and (wanted | generated) <= set(names)
        for item in z.infolist():
            content = z.read(item)
            assert (item.external_attr >> 16) & 0o777 == 0o644 and item.date_time == (1980, 1, 1, 0, 0, 0), item.filename
            git_id = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
            assert git_id == files[item.filename], item.filename
            if item.filename not in wanted | generated:
                continue
            info = {"sha256": sha(content), "bytes": len(content), "gitBlob": git_id}
            if item.filename in wanted:
                assert git("show", src + ":" + item.filename) == content
            else:
                assert (Path("bazel-bin") / item.filename).read_bytes() == content, item.filename
                if item.filename.endswith(".so"):
                    assert content[:6] == b"\x7fELF\x02\x01"
                    machine = int.from_bytes(content[18:20], "little")
                    assert machine == (62 if "amd64" in item.filename else 183)
                    info["machine"] = machine
            result["selected"][item.filename] = info
    result["completeGitTreeMatches"] = True
    save()


def check_actions():
    finished, exports, machines = [], [], set()
    for line in (work / "events.jsonl").open():
        event = json.loads(line)
        if "started" in event:
            result["bazelInvocation"] = event["started"]["uuid"]
        if "finished" in event:
            finished.append(event["finished"]["exitCode"].get("code", 0))
        if "action" not in event:
            continue
        action = event["action"]
        ident = event["id"]["actionCompleted"]
        if ident.get("label", "").removeprefix("@@") == "//:go_export" and action.get("type") == "GoExport" and action.get("success"):
            exports.append(ident["primaryOutput"])
        if ident.get("label", "").removeprefix("@@") == "//vdso:vdso.so" and action.get("type") == "CppCompile" and action.get("success"):
            machines.add(ident["configuration"]["id"])
    result.update(completedExitCodes=finished, goExportActions=exports, vdsoCompileConfigurations=sorted(machines))
    save()
    assert finished == [0] and len(exports) == 1 and exports[0].endswith("/go_export.zip")
    assert (Path(exports[0])).samefile("bazel-bin/go_export.zip") and len(machines) >= 2
    compilers = {}
    for config in ("x86_64", "aarch64"):
        data = subprocess.check_output(["bazel", "aquery", "--config=" + config, "--output=jsonproto", "--include_commandline", "--include_param_files",
                                        "mnemonic('CppCompile', //vdso:vdso.so)"])
        commands = [a["arguments"] + [v for p in a.get("paramFiles", []) for v in p.get("arguments", [])] for a in json.loads(data)["actions"]]
        drivers = sorted({argv[0] for argv in commands})
        triples = sorted({arg.split("=", 1)[1] if "=" in arg else argv[i+1] for argv in commands
                          for i, arg in enumerate(argv) if arg.startswith(("--target=", "-target=")) or arg in ("-target", "--target")})
        compilers[config] = {"drivers": drivers, "triples": triples}
        result["vdsoCompilers"] = compilers
        save()
        assert drivers and all("llvm" in name and name.split("/")[-1] in ("clang", "clang++") for name in drivers), drivers
        assert any(triple.startswith(config) for triple in triples), (config, triples)
    result["verified"] = True


try:
    assert git("rev-parse", "HEAD").decode().strip() == src and not git("remote")
    run_script()
    check_archive()
    check_actions()
except Exception as err:
    result["error"] = f"{type(err).__name__}: {str(err)[:600]}"
    raise
finally:
    save()
