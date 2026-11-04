"""Verify the optimized installed fileset and the Make installation on native workers."""

from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import stat
import sys
import traceback

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
PRIOR = {
    "amd64": "be7e1c8af10ba21dc9881d15b3c7c57879269f55e67ec609ed35aeb6ed9a8fec",
    "arm64": "5108438e45f485b9843daa27537921cf65ef71d1cc0878fec62cda5507ada09c",
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
    row = {"arch": arch, "source": q.PRODUCT, "started": q.now(), "status": "INCOMPLETE"}
    q.save(arch, row)
    exe = "//test/rbe/release_prewarmer:" + arch
    config = ["--config=" + {"amd64": "x86_64", "arm64": "aarch64"}[arch], "-c", "opt",
              "--extra_execution_platforms=" + exe, "--host_platform=" + exe,
              "--extra_toolchains=//test/rbe/go_bootstrap:linux_" + arch + "_ash",
              "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
              "--remote_accept_cached", "--remote_download_outputs=toplevel", "--jobs=32",
              "--modify_execution_info=^CopyFile=+no-remote-cache",
              "--lockfile_mode=off", "--color=no", "--curses=no"]
    try:
        with log.open("w") as output:
            run = subprocess.run(["timeout", "--signal=INT", "--kill-after=30", "4500",
                                  "bazel", *q.PROOF, "build", *config,
                                  "--build_event_json_file=" + str(bep), "--execution_log_compact_file=" + str(compact),
                                  STAGE], stdout=output, stderr=subprocess.STDOUT)
        row.update(native_exit=run.returncode, returned=q.now())
        if run.returncode:
            row["status"] = "FAIL"
        q.save(arch, row)
        q.PUBLIC = STAGE
        row["events"] = q.events(bep)
        row["diagnostics"] = q.diagnostics(log) if run.returncode else []
        q.save(arch, row)
        if compact.is_file():
            row["runners"], row["actions"] = actions(compact, arch)
        q.save(arch, row)
        if run.returncode:
            return row
        assert row["events"]["owners"] == [True] and row["events"]["exit"] == 0 and row["events"]["bazel"] == "9.3.1"
        root = Path("bazel-bin/release")
        targets = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
        assert targets == FILES
        row["binaries"] = binaries = {name: q.elf(root / name) for name in sorted(FILES)}
        q.save(arch, row)
        assert all(b["machine"] == {"amd64": 62, "arm64": 183}[arch] and b["load"] and b["entry"]
                   and os.access(root / name, os.X_OK) for name, b in binaries.items())
        assert binaries["gvisor-bin/gvisor-sentry-prewarmer"]["sha256"] == PRIOR[arch]
        matched, linked = set(), set()
        for a in row["actions"]:
            assert a["worker_matches"] and not a["status"] and not a["exit"]
            assert a["runner"] in ("remote", "remote cache hit")
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
        q.save("verdict", {"source": q.PRODUCT, "carrier": os.environ["GITHUB_SHA"], "verified": good,
                           "clean_source": clean, "expected": sorted(q.PIN), "executed": executed, "failed": failed,
                           "missing": missing, "execution_finished_at": max(done, default=None), "verified_at": q.now(),
                           "previous_eight_file_artifact": 13100369243})
        return int(not good)
    assert arch in q.PIN and q.git("rev-parse", "HEAD") == q.PRODUCT and not q.git("diff", "--name-only", "HEAD")
    return int(compiler_result(arch)["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
