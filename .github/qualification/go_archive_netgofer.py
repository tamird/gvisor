"""Build the archived netgofer with both Go export actions."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
from urllib.parse import unquote, urlsplit
import zipfile

import previous


WORK = previous.WORK
ROOT = Path.cwd()
GO = WORK / "go/bin/go"
GO_ENV = dict(os.environ, CGO_ENABLED="0", GOOS="linux", GOARCH="amd64",
              GOTOOLCHAIN="local", GOFLAGS="-mod=readonly", GOWORK="off",
              GOPROXY="https://proxy.golang.org", GOSUMDB="sum.golang.org")
ARMS = {
    "unmodified": ("10ebe13a71d7ee6b4c98e82839feebb08a2dd8ba", "Genrule"),
    "repaired": ("e48e4d8990d5c6eb242e5c2ad4c9315a78771977", "Genrule"),
    "direct": ("2d9319c56e3831e43045cc6da9f3634c8a0e9b39", "GoExport"),
}
MAIN = "runsc/cmd/netgofer/main.go"
LIB = "runsc/gofer/net/netgofer.go"
PREFIX = "runsc/gofer/net/"
SOURCE = "src/gvisor.dev/gvisor/"


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_archive(arm, mnemonic, row):
    bep = WORK / (arm + ".bep")
    args = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "build",
            "--jobs=4", "--lockfile_mode=off", "--remote_executor=", "--remote_cache=", "--bes_backend=",
            "--spawn_strategy=processwrapper-sandbox,local", "--strategy=GoStdlib=processwrapper-sandbox",
            "--strategy=GoStdlibList=processwrapper-sandbox", "--build_event_publish_all_actions",
            "--build_event_json_file=" + str(bep)]
    for attempt in range(32):
        inputs = ["--distdir=" + str(d) for d in sorted(previous.DIRS)]
        rc, data = previous.run(arm + "_build_" + str(attempt), [*args, *inputs, "//:go_export"], os.environ, 46)
        if not rc:
            break
        if not previous.recover(data.decode(errors="replace")):
            raise RuntimeError("Bazel could not build //:go_export; see original command diagnostics")
    assert rc == 0
    sets, output_sets, completed, finished = {}, [], [], []
    with bep.open() as f:
        for line in f:
            event = json.loads(line)
            ident = event["id"]
            if "namedSetOfFiles" in event:
                sets[ident["namedSet"]["id"]] = event["namedSetOfFiles"]
            if "targetCompleted" in ident and ident["targetCompleted"]["label"].removeprefix("@@") == "//:go_export":
                result = event["completed"]
                assert result["success"]
                row["configuration"] = ident["targetCompleted"]["configuration"]["id"]
                for group in result.get("outputGroup", []):
                    if group["name"] == "default":
                        output_sets.extend(s["id"] for s in group.get("fileSets", []))
            if "action" in event:
                action = event["action"]
                action_id = ident.get("actionCompleted", {})
                primary = action_id.get("primaryOutput", "")
                if primary.endswith("/go_export.zip") and action.get("type") == mnemonic:
                    completed.append({"type": mnemonic, "output": primary, "success": action.get("success", False),
                                      "owner": action_id.get("label", "").removeprefix("@@"),
                                      "configuration": action_id.get("configuration", {}).get("id")})
            if "started" in event:
                row["invocation"] = event["started"]["uuid"]
            if "finished" in event:
                finished.append(event["finished"]["exitCode"].get("code", 0))
    row["owner"] = "//:go_export"
    row["observed_actions"] = completed
    assert finished == [0] and row["configuration"]
    todo, files = list(output_sets), []
    while todo:
        group = sets[todo.pop()]
        files.extend(group.get("files", []))
        todo.extend(child["id"] for child in group.get("fileSets", []))
    item, = files
    uri = urlsplit(item["uri"])
    assert uri.scheme == "file" and not uri.netloc
    path = Path(unquote(uri.path))
    assert path.name == "go_export.zip" and path.samefile(ROOT / "bazel-bin/go_export.zip")
    matched = [a for a in completed if a["success"] and a["owner"] == row["owner"]
               and a["configuration"] == row["configuration"] and (ROOT / a["output"]).is_file()
               and path.samefile(ROOT / a["output"])]
    row["producer_matches_default"] = len(matched) == 1
    row["archive_bytes"], row["archive_sha256"] = path.stat().st_size, digest(path)
    assert not item.get("digest") or item["digest"] == row["archive_sha256"]
    assert int(item.get("length", row["archive_bytes"])) == row["archive_bytes"]
    previous.save()
    return path


def export(arm, head, archive, row):
    directory = WORK / ("module-" + arm)
    directory.mkdir()
    try:
        with zipfile.ZipFile(archive) as z:
            members = z.infolist()
            names = {m.filename for m in members}
            assert len(names) == len(members) and {"go.mod", "go.sum", "README.md"} <= names
            assert "MODULE.bazel" not in names and "BUILD" not in names
            for m in members:
                p = PurePosixPath(m.filename)
                assert not p.is_absolute() and ".." not in p.parts and m.external_attr >> 16 & 0o170000 == 0o100000
            row["archive_member_count"] = len(names)
            code = {n: previous.sha(z.read(n)) for n in sorted(names) if n.startswith(PREFIX) and n.endswith(".go")}
            row["implementation"] = code
            row["entrypoint_sha256"] = previous.sha(z.read(MAIN)) if MAIN in names else None
            if arm == "unmodified":
                assert LIB not in code and MAIN not in names
            else:
                assert previous.git("show", head + ":" + MAIN) == z.read(MAIN)
                assert previous.git("show", head + ":" + LIB) == z.read(LIB)
                with zipfile.ZipFile(ROOT / "bazel-bin/gopath.zip") as gopath:
                    expected = {n[len(SOURCE):]:previous.sha(gopath.read(n)) for n in gopath.namelist()
                                if n.startswith(SOURCE + PREFIX) and n.endswith(".go")}
                assert code == expected and LIB in code
                assert {PREFIX+"filter/filter.go", PREFIX+"egressfilter/egressfilter.go"} <= code.keys()
            z.extractall(directory)
        previous.save()
        os.chdir(directory)
        binary = WORK / ("runsc-netgofer-" + arm)
        rc, output = previous.run(arm + "_go_build", [str(GO), "build", "-o", str(binary), "./runsc/cmd/netgofer"], GO_ENV, 15)
        if arm == "unmodified":
            assert rc and b"runsc/cmd/netgofer" in output and b"directory not found" in output and not binary.exists()
            return
        assert rc == 0
        elf = binary.read_bytes()
        assert elf[:6] == b"\x7fELF\x02\x01" and int.from_bytes(elf[18:20],"little") == 62
        mod = subprocess.check_output([GO,"version","-m",binary], env=GO_ENV)
        assert b"\tpath\tgvisor.dev/gvisor/runsc/cmd/netgofer\n" in mod and b"\tbuild\tCGO_ENABLED=0\n" in mod
        rc, help_text = previous.run(arm + "_help", [str(binary), "help", "netgofer"], GO_ENV, 1)
        assert rc == 0 and b"netgofer [flags] <sandbox ID>" in help_text and b"-uds-fd" in help_text and 0 < len(help_text) < 65536
        (previous.OUT / (arm+"-help.txt")).write_bytes(help_text)
        row["binary"] = {"sha256": previous.sha(elf), "bytes": len(elf), "machine": 62, "help_sha256": previous.sha(help_text)}
    finally:
        os.chdir(ROOT)
        shutil.rmtree(directory)


def main():
    report = previous.REPORT
    report.clear()
    report.update(arms={}, assets={}, verified=False, prior_artifact=13625092477,
                  go=subprocess.check_output([GO,"version"], text=True, env=GO_ENV).strip())
    for arm, (head, mnemonic) in ARMS.items():
        row = report["arms"][arm] = {"source":head, "verified":False}
        try:
            previous.git("switch", "-q", "--detach", head)
            assert previous.git("rev-parse","HEAD").decode().strip() == head
            subprocess.run(["git","diff","--exit-code","HEAD"],check=True)
            archive = build_archive(arm, mnemonic, row)
            export(arm, head, archive, row)
            row["consumer_verified"] = True
            assert row["producer_matches_default"], "no unique successful owner/config/output action for this physical ZIP"
            row["verified"] = True
        except Exception as exc:
            row["error"] = type(exc).__name__+" "+str(exc)[:400]
        previous.save()
    a,b = (report["arms"][n] for n in ("repaired","direct"))
    if a["verified"] and b["verified"]:
        assert a["entrypoint_sha256"] == b["entrypoint_sha256"] and a["implementation"] == b["implementation"]
        report["same_archive_bytes"] = a["archive_sha256"] == b["archive_sha256"]
    report["verified"] = all(r["verified"] for r in report["arms"].values())
    previous.save()
    assert report["verified"]


if __name__ == "__main__":
    main()
