"""Build the unchanged release and guest, then the independent reporting tool."""

import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import struct
import subprocess

import public_assets as assets

from measure import BUNDLE, INSTALL, OUT, SOURCE, WORK, digest


GUEST = "//test/rbe/restore_advice:guest"
BENCHSTAT = "@org_golang_x_perf//cmd/benchstat:benchstat"
PRIVATE = WORK / "asset-prepare"
FLAGS = ["--config=x86_64", "-c", "opt", "--jobs=4", "--lockfile_mode=off",
         "--distdir=" + str(WORK / "dist"),
         "--noremote_accept_cached", "--noremote_upload_local_results", "--remote_executor=", "--bes_backend=", "--spawn_strategy=linux-sandbox",
         "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
         "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox"]
BAZEL = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g", "--bazelrc=" + str(PRIVATE / "service.rc")]


def wanted(text):
    found = {}
    for line in text.splitlines():
        if not any(message in line.lower() for message in ("error downloading", "download from ", "failed to fetch")):
            continue
        mods = set(assets.DIR.findall(line))
        for url in {match.group(0) for match in assets.URL.finditer(line)}:
            owners = found.setdefault(url, set())
            if len(mods) == 1:
                owners.update(mods)
    return found


def recover(urls, record):
    progress = False
    for url, owners in sorted(urls.items()):
        if len(record["assets"]) >= 40 or sum(a["bytes"] for a in record["assets"]) >= 400 << 20:
            break
        try:
            result = assets.tool_asset(url)
            if result is None and len(owners) == 1:
                module, = owners
                version = assets.URL.fullmatch(url).group(3).removeprefix("v")
                result = assets.acquire(module, version, wanted_url=url)
        except (AssertionError, ValueError, KeyError, OSError):
            result = None
        if result:
            record["assets"].append(result)
            progress = True
    return progress


def run(argv, phase, expected):
    report_path = PRIVATE / "assets.json"
    record = json.loads(report_path.read_bytes())
    for index in range(1, 25):
        log = PRIVATE / "bazel.log"
        bep = PRIVATE / "build.bep"
        bep.unlink(missing_ok=True)
        with log.open("wb") as dst:
            rc = subprocess.run([*argv, "--build_event_json_file=" + str(bep), *expected],
                                stdout=dst, stderr=subprocess.STDOUT).returncode
        text = log.read_text(errors="replace")
        urls = wanted(text)
        row = {"phase": phase, "attempt": index, "exit": rc, "log_sha256": digest(log),
               "failed_release_urls": {k: sorted(v) for k, v in sorted(urls.items())[:20]},
               "error_classes": [s for s in ("no such target", "no such package", "Error downloading",
                                             "missing strict dependencies", "No space left", "out of memory",
                                             "no registered toolchains", "GoCompilePkg") if s.lower() in text.lower()]}
        # Preserve the exact public URL path for other archive forms, discarding
        # query strings, malformed paths, and any URI on a private host.
        public_urls = set(re.findall(
            r"https://(?:github[.]com|dl[.]google[.]com|go[.]dev|bcr[.]bazel[.]build|mirror[.]bazel[.]build|storage[.]googleapis[.]com)/[A-Za-z0-9_+%./~-]+",
            "\n".join(line for line in text.splitlines() if "download" in line.lower() and "error" in line.lower())))
        row["other_public_urls"] = sorted(u[:400] for u in public_urls if u not in urls)[:12]
        row["failed_repositories"] = sorted(set(re.findall(
            r"fetch of repository '([A-Za-z0-9_.+~-]{1,180})'", text)) | {
                repo for line in text.splitlines() if "Error downloading" in line
                for repo in re.findall(r"/external/([A-Za-z0-9_.+~-]{1,180})/", line)
            })[:16]
        if bep.is_file():
            counts = {}
            completed = []
            for line in bep.open():
                event = json.loads(line)
                if "completed" in event:
                    owner = event["id"].get("targetCompleted", {}).get("label", "")
                    if owner in expected or owner.endswith("//cmd/benchstat:benchstat"):
                        completed.append({"owner": owner, "success": event["completed"].get("success", False)})
                runners = event.get("buildMetrics", {}).get("actionSummary", {}).get("runnerCount", [])
                counts.update({r["name"]: r["count"] for r in runners})
            row.update(runners=counts, named_targets=completed)
        record["attempts"].append(row)
        report_path.write_text(json.dumps(record, indent=2) + "\n")
        if not rc:
            assert not any(n and "remote" in k.lower() for k, n in row.get("runners", {}).items()), row
            assert len(row.get("named_targets", [])) == len(expected) and all(t["success"] for t in row["named_targets"]), row
            return
        if index == 24 or not recover(urls, record):
            raise RuntimeError((phase, rc, row["error_classes"], row["log_sha256"]))
        report_path.write_text(json.dumps(record, indent=2) + "\n")


def exe(target):
    files = subprocess.check_output([*BAZEL, "cquery", *FLAGS, "--output=files", target], text=True)
    candidates = [Path(n) for n in files.splitlines() if Path(n).is_file() and os.access(n, os.X_OK)]
    assert len(candidates) == 1, target
    return candidates[0]


def build():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    subprocess.run(["git", "diff", "--quiet", "HEAD"], check=True)
    original_guest = Path("test/rbe/restore_advice/guest.go")
    assert subprocess.check_output(["git", "hash-object", original_guest], text=True).strip() == "fa8722188274a32ffecd31a55da785c613585fc8"
    doc = {"source": SOURCE, "build_verified": False}
    out = PRIVATE / "build.json"
    assets.DIST = WORK / "dist"
    record = json.loads((OUT / "public-seeds.json").read_bytes())
    record.update(attempts=[], stored_source=SOURCE, remote_execution=False, remote_action_cache=False,
                  remote_asset_endpoint="grpcs://remote.buildbuddy.io")
    (PRIVATE / "assets.json").write_text(json.dumps(record, indent=2) + "\n")
    try:
        run([*BAZEL, "build", *FLAGS], "release and guest", ["//:release", GUEST])
        shutil.copytree("bazel-bin/release", INSTALL)
        root = BUNDLE / "rootfs"
        root.mkdir(parents=True)
        shutil.copy2(exe(GUEST), root / "probe")
        names = {"runsc", "containerd-shim-runsc-v1", *("gvisor-bin/" + n for n in
                 ("checkpointgofer","gvisor-proc-umounter","gvisor-sentry-prewarmer","gvisor_sentry",
                  "runsc-fd-parking","runsc-metric-server","runsc-netgofer"))}
        assert {str(p.relative_to(INSTALL)) for p in INSTALL.rglob("*") if p.is_file()} == names
        doc["release"] = {n: digest(INSTALL / n) for n in sorted(names)}
        for path in [root / "probe", *(INSTALL / n for n in names)]:
            with path.open("rb") as f:
                h = f.read(20)
            assert h[:7] == b"\x7fELF\x02\x01\x01" and struct.unpack_from("<H", h, 18)[0] == 62
        doc["guest"] = digest(root / "probe")
        version = subprocess.check_output([INSTALL / "runsc", "--version"], text=True)
        stamp = subprocess.check_output(["git", "describe", "--always", "--tags", "--exclude",
                                         "release-*-staging", "--abbrev=12", "--dirty"], text=True).strip()
        assert stamp != "0.0.0" and stamp in version
        doc["version"] = version.splitlines()[0]
        spec = dict(ociVersion="1.0.2", root=dict(path="rootfs"), process=dict(terminal=False,
                    user=dict(uid=0,gid=0), cwd="/", args=["/probe"], env=["PATH=/","GOMAXPROCS=2"]),
                    mounts=[dict(type="proc",source="proc",destination="/proc")],
                    linux=dict(namespaces=[dict(type=t) for t in ("pid","mount","ipc","uts","network")]))
        (BUNDLE / "config.json").write_text(json.dumps(spec))
        (WORK / "runtime").mkdir()
        out.write_text(json.dumps(doc) + "\n")
        module = Path("MODULE.bazel")
        original = module.read_bytes()
        try:
            additions = (WORK / "benchstat.module").read_bytes()
            module.write_bytes(original + b"\n" + additions)
            run([*BAZEL, "build", *FLAGS], "benchstat", [BENCHSTAT])
            shutil.copy2(exe(BENCHSTAT), WORK / "benchstat")
        finally:
            module.write_bytes(original)
        doc["benchstat_sha256"] = digest(WORK / "benchstat")
        doc["benchstat_module_sha256"] = hashlib.sha256(additions).hexdigest()
        doc["build_verified"] = True
    except Exception as exc:
        doc["failure"] = type(exc).__name__ + " " + str(exc)[:450]
        raise
    finally:
        out.write_text(json.dumps(doc, indent=2) + "\n")


if __name__ == "__main__":
    build()
