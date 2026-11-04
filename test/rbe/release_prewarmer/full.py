"""Verify the real release packages and their contents on both native workers."""

from collections import Counter
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import traceback

import qualify as q

STAGE = "//debian:release-packages"
PACKAGE_NAMES = {"gvisor.tar.bz2", "gvisor.tar.zstd", "runsc.deb"}
WRITERS = {"//debian:debian": "MakeDeb", "//debian:gvisor-release-tar-bz2": "PackageTar",
           "//debian:gvisor-release-tar-zstd": "PackageTar"}
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
  (if ((.mnemonic|IN("PackageTar","MakeDeb","PackageVersion")) or
       (.exitCode//0)!=0 or (.status//"")!="") then
    {label:.targetLabel,mnemonic,executable:.args[0],digest,outputs,
     temp:[.envVars[]? | select(.name|IN("TMP","TMPDIR","TEMP","GOTMPDIR")) | .name]}
   else {} end)
elif .file and (.file.path|test("/bin/(debian|shim)/")) then
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
        with subprocess.Popen(["jq", "-c", SELECT], stdin=reader.stdout, stdout=subprocess.PIPE,
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
                    if len(selected) < 100:
                        selected.append(item)
            assert rows.wait() == 0
        assert reader.wait() == 0
    for item in selected:
        item["outputs"] = [files[o["outputId"]] for o in (item.pop("outputs", None) or [])
                           if o.get("outputId") in files]
    return dict(counts), selected


def scan_tar(fp, expected):
    """Read each regular file once; never extract or execute package contents."""
    files = {}
    with tarfile.open(fileobj=fp, mode="r|*") as tf:
        for member in tf:
            name = member.name.removeprefix("./").rstrip("/")
            assert not name.startswith("/") and ".." not in name.split("/")
            assert member.uid == member.gid == 0 and not (member.mode & 0o7000)
            if member.isdir():
                assert name in ("", ".") or any(p.startswith(name + "/") for p in expected)
                assert member.mode == 0o755
                continue
            assert member.isreg() and name in expected and name not in files
            mode, want, elf = expected[name]
            assert member.mode == mode
            stream = tf.extractfile(member)
            head = stream.read(64)
            h = hashlib.sha256(head)
            for block in iter(lambda: stream.read(1 << 20), b""):
                h.update(block)
            sha = h.hexdigest()
            row = {"uid": member.uid, "gid": member.gid, "mode": member.mode,
                   "size": member.size, "sha256": sha}
            if elf:
                assert head[:6] == b"\x7fELF\x02\x01" and int.from_bytes(head[18:20], "little") == elf
                row["elf_machine"] = elf
            files[name] = row
            assert sha == want
    assert set(files) == set(expected)
    return files


def scan_command(argv, expected):
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as cmd:
        try:
            result = scan_tar(cmd.stdout, expected)
            while cmd.stdout.read(1 << 20):
                pass
        finally:
            cmd.stdout.close()
        assert cmd.wait() == 0
    return result


def inspect_package(path, binaries, arch, result):
    machine = {"amd64": 62, "arm64": 183}[arch]
    files = {name: (0o755, binaries[name]["sha256"], machine) for name in FILES}
    if path.suffix != ".deb":
        if path.suffix == ".zstd":
            result["members"] = scan_command(["zstd", "-dqc", str(path)], files)
        else:
            with path.open("rb") as fp:
                result["members"] = scan_tar(fp, files)
        return
    version = subprocess.check_output(["tools/workspace_status.sh"], text=True).strip().split(" ", 1)[1]
    version = re.sub(r"^[^0-9]+", "", version)
    control = Parser().parsestr(subprocess.check_output(["dpkg-deb", "-f", str(path)], text=True))
    fields = {"Package": "runsc", "Architecture": arch, "Version": version,
              "Homepage": "https://gvisor.dev/", "Maintainer": "The gVisor Authors <gvisor-dev@googlegroups.com>"}
    assert {key: control[key] for key in fields} == fields
    result["control"] = fields
    data = {"usr/bin/" + key: val for key, val in files.items()}
    data["etc/containerd/runsc.toml"] = (0o644, hashlib.sha256(Path("shim/runsc.toml").read_bytes()).hexdigest(), 0)
    result["members"] = scan_command(["dpkg-deb", "--fsys-tarfile", str(path)], data)
    with subprocess.Popen(["dpkg-deb", "--ctrl-tarfile", str(path)], stdout=subprocess.PIPE) as proc:
        meta = {}
        with tarfile.open(fileobj=proc.stdout, mode="r|*") as tf:
            for member in tf:
                if member.isdir():
                    continue
                name = member.name.removeprefix("./")
                assert name in {"control", "conffiles", "changelog", "postinst"} and name not in meta
                assert member.isreg() and member.uid == member.gid == 0 and member.size < 1 << 20
                assert member.mode == (0o755 if name == "postinst" else 0o644)
                raw = tf.extractfile(member).read()
                if name == "conffiles":
                    assert raw.splitlines() == [b"/etc/containerd/runsc.toml"]
                if name == "postinst":
                    assert raw == Path("debian/postinst.sh").read_bytes()
                if name == "changelog":
                    assert raw.splitlines()[0] == f"runsc ({version}) unstable; urgency=medium".encode()
                    assert b"Thu, 01 Jan 1970 00:00:00 +0000" in raw
                meta[name] = {"mode": member.mode, "sha256": hashlib.sha256(raw).hexdigest()}
        while proc.stdout.read(1 << 16):
            pass
        assert proc.wait() == 0 and set(meta) == {"control", "conffiles", "changelog", "postinst"}
        result["control_members"] = meta


def diagnostics(path):
    classes = ("not found", "no such file", "not executable", "permission denied",
               "no matching toolchains", "no such target", "no such package", "visibility",
               "not defined", "keyerror", "importerror", "modulenotfounderror", "traceback",
               "_bz2", "_lzma", "zstd", "/bin/bash", "/usr/bin/env", "exit 1", "exit 127",
               "failed to fetch", "timed out", "timeout", "unauthenticated", "resource_exhausted",
               "remote cache", "analysis of target", "compilation failed", "glibc", "unsupported",
               "error executing", "exec format", "executablewrapper", "patch failed")
    files = {"BUILD.bazel", "BUILD", "MODULE.bazel", "native_launcher.bzl", "py_executable.bzl",
             "make_deb.py", "tar.bzl", "build_tar.py", "changelog.go", "main.go", "compressor.c",
             "go.bzl", "slices.go", "deps.bzl", "extensions.bzl", "platforms.bzl"}
    reasons, locations = set(), set()
    for line in path.open(errors="replace"):
        text = line[:8192].lower()
        reasons.update(c for c in classes if c in text)
        if "error" in text or "traceback" in text:
            for match in re.finditer(r"(?:^|/)([a-zA-Z0-9._-]{1,60}):(\d{1,7})(?::(\d{1,7}))?", line[:8192]):
                if match[1] in files:
                    locations.add(f"{match[1]}:{match[2]}")
    return {"classes": sorted(reasons), "locations": sorted(locations)[:30]}


def compiler_result(arch):
    bep, compact, log = [q.TMP / (arch + x) for x in (".bep", ".compact", ".log")]
    row = {"prior_install_artifact": 13292873920, "arch": arch, "source": q.PRODUCT, "started": q.now(), "status": "INCOMPLETE"}
    q.save(arch, row)
    exe = "//test/rbe/release_prewarmer:" + arch
    config = ["--config=" + {"amd64": "x86_64", "arm64": "aarch64"}[arch], "-c", "opt",
              "--extra_execution_platforms=" + exe, "--host_platform=" + exe,
              "--extra_toolchains=//test/rbe/go_bootstrap:linux_" + arch + "_ash,//test/rbe/protoc_check:" + ("static_python" if arch == "amd64" else "static_python_arm64"),
              "--@rules_python//python/config_settings:bootstrap_impl=native",
              "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
              "--remote_accept_cached", "--remote_download_outputs=toplevel", "--jobs=32",
              "--modify_execution_info=^(MakeDeb|PackageTar|PackageVersion)$=+no-remote-cache",
              "--lockfile_mode=off", "--color=no", "--curses=no"]
    try:
        prior = json.loads((q.TMP / "prior" / (arch + ".json")).read_text())
        assert prior["source"] == "96057fa928e75e6ff35836902bd4e73297ff1aa9" and prior["status"] == "PASS"
        assert set(prior["binaries"]) == FILES
        with log.open("w") as output:
            run = subprocess.run(["timeout", "--signal=INT", "--kill-after=30", "4100",
                                  "bazel", *q.PROOF, "build", *config,
                                  "--build_event_json_file=" + str(bep), "--execution_log_compact_file=" + str(compact),
                                  "--keep_going", STAGE, "//:release"], stdout=output, stderr=subprocess.STDOUT)
        row.update(native_exit=run.returncode, returned=q.now())
        if run.returncode:
            row["status"] = "FAIL"
        q.save(arch, row)
        q.PUBLIC = STAGE
        row["events"] = q.events(bep)
        row["diagnostics"] = diagnostics(log) if run.returncode else []
        q.save(arch, row)
        if compact.is_file():
            row["runners"], row["actions"] = actions(compact, arch)
        q.save(arch, row)
        assert not row["runners"].get("different_platform")
        found = {}
        for action in row["actions"]:
            if WRITERS.get(action["label"]) != action["mnemonic"]:
                continue
            if action["exit"] or action["status"]:
                continue
            assert action["worker_matches"] and action["runner"] == "remote" and not action["cache"]
            paths = {Path(f["path"]).name: f["digest"] for f in action["outputs"]}
            for name in PACKAGE_NAMES & paths.keys():
                assert name not in found
                found[name] = paths[name]
        root = Path("bazel-bin/release")
        canonical = {str(p.relative_to(root)): q.elf(p) for p in root.rglob("*") if p.is_file()}
        row["resolved_files"] = sorted(canonical)
        row["prior_bytes_match"] = {n: d == prior["binaries"][n] for n, d in canonical.items() if n in FILES}
        q.save(arch, row)
        package_dir = Path("bazel-bin/debian")
        row["packages"] = {}
        for name in sorted(found):
            path = package_dir / name
            if not path.is_file():
                continue
            with path.open("rb") as fp:
                actual = hashlib.file_digest(fp, "sha256").hexdigest()
            assert actual == found[name]["hash"] and path.stat().st_size == int(found[name]["sizeBytes"])
            result = {"sha256": actual, "bytes": path.stat().st_size, "verified": False}
            row["packages"][name] = result
            q.save(arch, row)
            try:
                inspect_package(path, canonical, arch, result)
                result["verified"] = True
            except (AssertionError, OSError, KeyError, tarfile.TarError, subprocess.SubprocessError) as err:
                result.update(check_error=type(err).__name__, line=traceback.extract_tb(err.__traceback__)[-1].lineno)
            q.save(arch, row)
        if run.returncode:
            return row
        assert row["events"]["owners"] == [True] and row["events"]["exit"] == 0 and row["events"]["bazel"] == "9.3.1"
        assert set(row["packages"]) == PACKAGE_NAMES and all(p["verified"] for p in row["packages"].values())
        assert set(canonical) == FILES
        assert all(row["prior_bytes_match"][n] for n in FILES if n.endswith(("prewarmer","fd-parking","proc-umounter")))
        row["status"] = "PASS"
    except (AssertionError, KeyError, ValueError, OSError, tarfile.TarError, subprocess.SubprocessError) as err:
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
                           "prior_nine_member_install_artifact": 13292873920})
        return int(not good)
    assert arch in q.PIN and q.git("rev-parse", "HEAD") == q.PRODUCT and not q.git("diff", "--name-only", "HEAD")
    return int(compiler_result(arch)["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
