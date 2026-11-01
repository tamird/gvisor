"""Verify the optimized installed fileset and its five production Go links."""

from collections import Counter
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import traceback

import qualify as q

STAGE = "//:release"
GUEST = "@bootstrap_busybox//:payload/usr/bin/busybox"
LINKS = {
    "//runsc:runsc": "runsc",
    "//shim:containerd-shim-runsc-v1": "containerd-shim-runsc-v1",
    "//runsc/cmd/sentry:gvisor_sentry": "gvisor-bin/gvisor_sentry",
    "//runsc/checkpointgofer:checkpointgofer_binary": "gvisor-bin/checkpointgofer",
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
  {kind:"action",runner,cache:(.cacheHit//false),exit:(.exitCode//0),status,platform:
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
              "--remote_accept_cached", "--remote_download_outputs=toplevel", "--jobs=160",
              "--lockfile_mode=off", "--color=no", "--curses=no"]
    try:
        with log.open("w") as output:
            run = subprocess.run(["timeout", "--signal=INT", "--kill-after=30", "4500",
                                  "bazel", *q.PROOF, "build", *config,
                                  "--build_event_json_file=" + str(bep), "--execution_log_compact_file=" + str(compact),
                                  STAGE, *([GUEST] if arch == "amd64" else [])], stdout=output, stderr=subprocess.STDOUT)
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
        if arch == "amd64":
            cquery = subprocess.check_output(["bazel", *q.PROOF, "cquery", *config, "--output=files", GUEST], text=True)
            guest_path, = [x for x in cquery.splitlines() if x.endswith("/payload/usr/bin/busybox")]
            execroot = Path(subprocess.check_output(["bazel", *q.PROOF, "info", "execution_root"], text=True).strip())
            busy = q.TMP / "original-busybox"
            shutil.copy2(execroot / guest_path, busy)
            assert q.elf(busy)["sha256"] == "dbac288c29ba568459550a2da9e7ae0ded6b1fc728ee9fad3044c44e62d6ac14"
            shutil.copytree(root, q.TMP / "exact-release")
        row["status"] = "PASS"
    except (AssertionError, KeyError, ValueError, OSError, subprocess.SubprocessError) as err:
        row.update(collection_error=type(err).__name__, line=traceback.extract_tb(err.__traceback__)[-1].lineno)
    finally:
        row["verified_at"] = q.now()
        q.save(arch, row)
    return row


def smoke():
    # Reuse the native systrap launch from the original optimized release qualification.
    from guest import smoke as original, results
    assert "BUILDBUDDY_API_KEY" not in os.environ and platform.machine() == "x86_64"
    row = json.loads((q.OUT / "amd64.json").read_text())
    assert row["status"] == "PASS"
    install = q.TMP / "exact-release"
    assert {k: q.elf(install / k) for k in FILES} == row["binaries"]
    original(install, q.TMP / "original-busybox")
    assert results["native_smoke"]["verdict"] == "GUEST_EXECUTED"


def main():
    arch = sys.argv[1]
    if arch == "guest":
        smoke()
        return 0
    if arch == "verdict":
        outcomes = {arch: json.loads((q.OUT / (arch + ".json")).read_text()) for arch in q.PIN}
        guest = json.loads((q.OUT / "guest.json").read_text()) if (q.OUT / "guest.json").exists() else {}
        clean = q.git("rev-parse", "HEAD") == q.PRODUCT and not q.git("diff", "--name-only", "HEAD")
        good = clean and all(x["status"] == "PASS" for x in outcomes.values()) and guest.get("native_smoke", {}).get("verdict") == "GUEST_EXECUTED"
        q.save("verdict", {"source": q.PRODUCT, "carrier": os.environ["GITHUB_SHA"], "verified": good,
                           "clean_source": clean, "finished": q.now(), "guest": guest.get("native_smoke", {}).get("verdict"),
                           "status": {k: x["status"] for k, x in outcomes.items()},
                           "original_image_and_prewarmer_artifact": 13100242102})
        return int(not good)
    assert arch in q.PIN and q.git("rev-parse", "HEAD") == q.PRODUCT and not q.git("diff", "--name-only", "HEAD")
    return int(compiler_result(arch)["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
