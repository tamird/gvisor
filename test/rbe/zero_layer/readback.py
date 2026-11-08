"""Read back the two immutable zero-layer product builds on one native CPU."""

from collections import Counter
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import uuid

from qualification_report import file_record

TMP = Path(os.environ["RUNNER_TEMP"]) / "zero-layer"
STOCK = "5cfff701fff1611c5cc265e7d43d0478f4b78e9b"
FIXED = "960a5f77095b9ec423344e590b431ef60061d7ba"
ARCH = os.environ["PRODUCER_CPU"]
MACHINE, ELF, MANIFEST = {
    "amd64": ("x86_64", 62, "3d455195f1141b05cb8cc6deb07ea96b617e5436c7a259d9f3e7b1985f4d3f51"),
    "arm64": ("aarch64", 183, "0fe9f0d1914ac271ceffe2ff90d691c5a4dfcc2859928cdcf3561381a21ac5be"),
}[ARCH]
IMAGE = "docker://docker.io/tianon/scratch@sha256:" + MANIFEST
GO = "//pkg/gohacks:gohacks"
SYS = "//pkg/sentry/platform/systrap/sysmsg:"
ROOT = "//test/rbe/zero_layer:root"
STDLIB = "//test/rbe/zero_layer:stdlib"
ARCHIVE = "//test/rbe/zero_layer:gohacks_archive"
TARGETS = [ROOT, STDLIB, GO, ARCHIVE, SYS + "sighandler_object", SYS + "sighandler_data"]
FILES = {"root.txt", "runtime.a", "stdlib.pkg.json", "gohacks.a", "sighandler.built-in.bin.o",
         "sighandler.built-in.bin", "sighandler.go.arch", "gen_stub"}
FLAGS = ["--config=source-presubmit-mixed", "--config=" + MACHINE,
         "--host_platform=//test/rbe:scratch_build_" + ARCH,
         "--extra_execution_platforms=//test/rbe:scratch_build_" + ARCH,
         "--remote_default_exec_properties=Arch=" + ARCH,
         "--remote_default_exec_properties=container-image=" + IMAGE,
         "--spawn_strategy=remote", "--noremote_local_fallback", "--noremote_accept_cached",
         "--remote_download_outputs=toplevel", "--build_event_publish_all_actions",
         "--remote_download_regex=.*/sysmsg/.*/sighandler[.]built-in[.]bin[.]o$",
         "--noremote_upload_local_results", "--jobs=24", "--keep_going", "--color=no", "--curses=no"]

# From the original Go temp and docprocessor compact-log readers. Keep only the
# requested producer classes, their output IDs and their declared temp variables.
PROJECTION = r'''
if .file and (.file.path|test("/(root[.]txt|stdlib[.]pkg[.]json|gohacks[.]a|sighandler[.]built-in[.]bin([.]o)?|sighandler[.]go[.]arch|gen_stub)$")) then
  {id, files:[.file]}
elif .directory and (.directory.path|endswith("/pkg")) then
  {id, archives:([.directory.files[]?|select(.path|endswith(".a"))]|length),
   files:[.directory as $d | $d.files[]? | select(.path|endswith("/runtime.a")) | .path=($d.path+"/"+.path)]}
elif .spawn then .spawn | select((.targetLabel // "" | endswith("//pkg/sentry/platform/systrap/sysmsg:sighandler_data")) or
  (.mnemonic|IN("ObserveWorkerRoot","GoToolchainBinaryBuild","GoStdlibList","GoStdlib","GoCompilePkg","CppCompile","CppLink","GoLink"))) |
  {owner:.targetLabel, mnemonic, runner, cached:(.cacheHit//false), exit:(.exitCode//0), status:(.status//""),
   hash:.digest.hash, ids:[.outputs[]?.outputId // empty], ash:((.args[0]//"")|endswith("/test/rbe/go_bootstrap/ash")),
   temp:[.envVars[]?|select(.name|IN("TMPDIR","GOTMPDIR","TMP","TEMP"))|.name],
   goarch:([.envVars[]?|select(.name=="GOARCH")|.value][0]),
   platform:([.platform.properties[]?|select(.name|IN("Arch","OSFamily","container-image","workload-isolation-type"))]|from_entries)}
else empty end
'''


def capture(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def write(report: dict) -> None:
    path = TMP / "result.json"
    blob = json.dumps(report, indent=2, sort_keys=True)
    assert len(blob) < 180_000 and not re.search(r"(?i)authorization:|x-buildbuddy-api-key|github_pat_|gh[pousr]_", blob)
    path.write_text(blob + "\n")


def events(path: Path) -> dict:
    result = {"targets": {}, "actions": Counter(), "action_configs": {}, "failed_actions": []}
    sets, defaults = {}, {}
    for line in path.open():
        event = json.loads(line)
        ident = event["id"]
        if "started" in event:
            result["invocation"] = str(uuid.UUID(event["started"]["uuid"]))
        elif "finished" in event:
            result["exit"] = event["finished"]["exitCode"].get("code", 0)
        elif "namedSetOfFiles" in event:
            group = event["namedSetOfFiles"]
            paths = [Path(*f.get("pathPrefix", ()), f["name"]).as_posix() for f in group.get("files", ())
                     if Path(f["name"]).name in FILES | {"pkg"}]
            sets[ident["namedSet"]["id"]] = paths, [s["id"] for s in group.get("fileSets", ())]
        elif (owner := ident.get("targetCompleted")) and not owner.get("aspect") and owner["label"] in TARGETS:
            result["targets"][owner["label"]] = {"built": event.get("completed", {}).get("success", False),
                                                "config": owner["configuration"]["id"]}
            defaults[owner["label"]] = [s["id"] for g in event.get("completed", {}).get("outputGroup", ())
                                        if g["name"] == "default" for s in g.get("fileSets", ())]
        elif (owner := ident.get("actionCompleted")) and (action := event.get("action")):
            label = owner.get("label", "").lstrip("@")
            if label.startswith(SYS) or label in (ROOT, GO) or label.endswith("//:stdlib") or action.get("type") == "GoToolchainBinaryBuild":
                result["actions"][action["type"]] += 1
                if owner.get("primaryOutput"):
                    result["action_configs"][owner["primaryOutput"]] = (
                        action["type"], owner.get("configuration", {}).get("id"), bool(action.get("success")))
                    if not action.get("success"):
                        result["failed_actions"].append({"owner": label, "mnemonic": action["type"],
                            "config": owner.get("configuration", {}).get("id"), "output": owner["primaryOutput"]})
    for label, pending in defaults.items():
        seen, paths = set(), set()
        while pending:
            identifier = pending.pop()
            if identifier not in seen:
                seen.add(identifier)
                if identifier not in sets:
                    result["targets"][label]["missing_file_set"] = True
                    continue
                files, nested = sets[identifier]
                paths.update(files)
                pending.extend(nested)
        result["targets"][label]["outputs"] = sorted(paths)
    return result


def projection(path: Path) -> list[dict]:
    entries = {}
    rows = []
    with subprocess.Popen([TMP / "decoder", "print", "--raw", "--compact_execution_log=" + str(path)],
                          stdout=subprocess.PIPE) as decoder:
        with subprocess.Popen(["jq", "-c", PROJECTION], stdin=decoder.stdout, stdout=subprocess.PIPE, text=True) as jq:
            decoder.stdout.close()
            for line in jq.stdout:
                entry = json.loads(line)
                if "id" in entry:
                    entries[str(entry["id"])] = entry
                    continue
                owner = entry["owner"].lstrip("@")
                if (owner.startswith(SYS) or owner in (ROOT, GO) or owner.endswith("//:stdlib")
                        or entry["mnemonic"] == "GoToolchainBinaryBuild"):
                    rows.append(entry)
            assert jq.wait() == 0
        assert decoder.wait() == 0
    for row in rows:
        items = [entries[str(i)] for i in row.pop("ids") if str(i) in entries]
        row["archives"] = sum(item.get("archives", 0) for item in items)
        row["files"] = [file for item in items for file in item["files"] if Path(file["path"]).name in FILES]
        props = row.pop("platform")
        image_hash = re.search(r"@sha256:([0-9a-f]{64})$", props.get("container-image", ""))
        row["worker"] = {"cpu": props.get("Arch") if props.get("Arch") in ("amd64", "arm64") else "other",
                         "linux": props.get("OSFamily") == "linux", "oci": props.get("workload-isolation-type") == "oci",
                         "manifest": image_hash[1] if image_hash else None}
        row["native_worker"] = (props.get("Arch") == ARCH and props.get("OSFamily") == "linux"
                                and props.get("container-image") == IMAGE and props.get("workload-isolation-type") == "oci")
        row["fresh_remote"] = bool(row["native_worker"] and row["runner"] == "remote" and not row["cached"]
                                   and re.fullmatch(r"[0-9a-f]{64}", row["hash"] or ""))
    return rows


def matching(rows: list[dict], mnemonic: str, owner: str) -> list[dict]:
    return [r for r in rows if r["mnemonic"] == mnemonic and owner in r["owner"]
            and r["fresh_remote"] and not r["exit"] and not r["status"]]


def materialized(rows: list[dict], name: str, defaults: list[str] | None = None) -> tuple[Path, dict]:
    for r in rows:
        for file in r["files"]:
            path = Path(file["path"])
            if defaults is not None and not any(path == Path(p) or Path(p) in path.parents for p in defaults):
                continue
            if path.name == name and not path.is_absolute() and ".." not in path.parts and path.is_file():
                meta = file_record(path)
                if meta["sha256"] == file["digest"]["hash"] and meta["bytes"] == int(file["digest"]["sizeBytes"]):
                    return path, meta
    raise ValueError("missing materialized output: " + name)


def go_archive(path: Path) -> bool:
    with path.open("rb") as f:
        if f.read(8) != b"!<arch>\n":
            return False
        while header := f.read(60):
            assert len(header) == 60 and header[58:] == b"\x60\n"
            length = int(header[48:58])
            if header[:16].strip().rstrip(b"/") in (b"__.PKGDEF", b"_go_.o"):
                head = f.read(min(180, length))
                if head.startswith(f"go object linux {ARCH} go1.27.3 ".encode()):
                    return True
                f.seek(-len(head), 1)
            f.seek(length + length % 2, 1)
    return False


def packages(path: Path) -> int:
    text = path.read_text()
    names = set()
    decoder = json.JSONDecoder()
    pos = 0
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
            continue
        package, pos = decoder.raw_decode(text, pos)
        if package.get("Standard") and package.get("GoFiles"):
            names.add(package["PkgPath"])
    assert {"fmt", "runtime"} <= names
    return len(names)


def validate(row: dict, rows: list[dict], case: str) -> dict:
    if case == "root":
        path, meta = materialized(matching(rows, "ObserveWorkerRoot", ROOT), "root.txt", row["bep"]["targets"][ROOT]["outputs"])
        values = path.read_text().splitlines()
        assert values[0] == MACHINE and values[1:4] == ["/bin/sh absent", "/usr/bin/cc absent", "/hello absent"]
        assert values[4:] in (["/tmp absent"], ["/tmp present"])
        return {**meta, "native": MACHINE, "tmp": values[4].split()[1]}
    if case == "bootstrap":
        sdk = matching(rows, "GoToolchainBinaryBuild", "go_sdk")
        assert sdk and all(r["ash"] for r in sdk)
        return {"actions": len(sdk)}
    if case in ("stdlib", "list", "package"):
        mnemonic, owner, name = {
            "stdlib": ("GoStdlib", "//:stdlib", "runtime.a"), "list": ("GoStdlibList", "//:stdlib", "stdlib.pkg.json"),
            "package": ("GoCompilePkg", GO, "gohacks.a")}[case]
        actions = [r for r in matching(rows, mnemonic, owner) if r["goarch"] == ARCH and not r["temp"]]
        target = ARCHIVE if case == "package" else STDLIB
        path, meta = materialized(actions, name, row["bep"]["targets"][target]["outputs"])
        if case == "list":
            return {**meta, "packages": packages(path)}
        assert go_archive(path)
        if case == "stdlib":
            meta["archives"] = max(r["archives"] for r in actions)
            assert meta["archives"] > 100
        return meta
    cpp = matching(rows, "CppCompile", SYS)
    linked = matching(rows, "CppLink", SYS)
    elf, meta = materialized(linked, "sighandler.built-in.bin.o")
    with elf.open("rb") as stream:
        head = stream.read(20)
    assert head[:6] == b"\x7fELF\x02\x01" and int.from_bytes(head[18:], "little") == ELF
    assert len(cpp) >= 4
    config = row["bep"]["action_configs"].get(str(elf))
    assert config and config[0] == "CppLink" and config[2] and config[1]
    if case == "compiler":
        return {**meta, "machine": ELF, "cpp_compile_actions": len(cpp), "link_config": config[1]}
    assert case == "stub"
    producer = [r for r in rows if r["owner"].lstrip("@") == SYS + "sighandler_data"
                and r["fresh_remote"] and not r["exit"] and not r["status"]]
    generated = row["bep"]["targets"][SYS + "sighandler_data"]["outputs"]
    blob, blob_meta = materialized(producer, "sighandler.built-in.bin", generated)
    offsets, _ = materialized(producer, "sighandler.go.arch", generated)
    section = TMP / "section.bin"
    subprocess.run(["objcopy", "--dump-section", ".crblob=" + str(section), elf, TMP / "readback.elf"], check=True)
    assert blob_meta["bytes"] and blob.read_bytes() == section.read_bytes()
    symbols = dict((k, int(v, 16)) for v, k in re.findall(r"^([\da-fA-F]+)\s+\w\s+(__export_\w+)$", capture("nm", "-n", str(elf)), re.M))
    constants = dict((k, int(v, 16)) for k, v in re.findall(r"Sighandler_blob_offset__(__export_\w+) = (0x\w+)", offsets.read_text()))
    assert constants == symbols and symbols and all(0 <= offset < blob_meta["bytes"] for offset in symbols.values())
    return {**blob_meta, "exported_offsets": len(symbols)}


def run_case(source: str, report: dict) -> None:
    row = report["sources"][source] = {"checks": {}, "stage": "checkout"}
    subprocess.run(["git", "checkout", "--quiet", "--detach", source], check=True)
    assert capture("git", "rev-parse", "HEAD") == source
    # Bazel may update this one tracked file on the disposable runner. Restore
    # the selected product's original before starting its measurement.
    subprocess.run(["git", "restore", "--worktree", "--", "MODULE.bazel.lock"], check=True)
    assert not capture("git", "diff", "--name-only", "HEAD")
    for name in ("test/rbe/zero_layer/BUILD", "test/rbe/zero_layer/outputs.bzl"):
        carrier = os.environ["GITHUB_SHA"]
        subprocess.run(["git", "restore", "--source=" + carrier, "--worktree", "--", name], check=True)
        assert capture("git", "hash-object", name) == capture("git", "rev-parse", carrier + ":" + name)
    assert Path(".bazelversion").read_text().strip() == "9.3.1" and capture(TMP / "bazel", "--version") == "bazel 9.3.1"
    # The two products intentionally reuse the same Bazel workspace. Clear the
    # local action record so an unchanged owner cannot satisfy a second fresh build.
    subprocess.run([TMP / "bazel", "clean"], check=True)
    stage = TMP / source
    stage.mkdir()
    command = ["timeout", "--signal=INT", "--kill-after=60s", "38m",
               TMP / "bazel", "--bazelrc=" + str(TMP / "remote.rc"), "build", *FLAGS,
               "--build_event_json_file=" + str(stage / "events"), "--execution_log_compact_file=" + str(stage / "spawns"), *TARGETS]
    row["stage"] = "build"
    write(report)
    with (stage / "console").open("w") as f:
        row["exit"] = subprocess.run(command, stdout=f, stderr=subprocess.STDOUT).returncode
    console = (stage / "console").read_text(errors="replace")
    missing_tmp = r"(?:mkdir|stat|open) /tmp\b[^\n:]*: no such file"
    row["diagnostics"] = [name for name, pattern in {
        "missing_tmp": missing_tmp,
        "image_pull": r"(?:could not|failed to) (?:pull|resolve) .*image",
        "overlay_mount": r"(?:mount overlay|overlayfs).*invalid argument",
        "clang_temp": r"unable to make temporary file|cannot create temporary file",
        "toolchain_resolution": r"no matching toolchains|no matching execution platform",
        "analysis": r"analysis of target .*failed",
        "remote_capacity": r"no registered executors|resource.exhausted"}.items() if re.search(pattern, console, re.I)]
    row["stage"] = "readback"
    write(report)
    row["bep"] = events(stage / "events")
    actions = projection(stage / "spawns")
    row["spawns"] = [{k: v for k, v in r.items() if k not in ("files", "status")} for r in actions]
    row["temp_failures"] = []
    errors = [block.splitlines()[0] for block in re.split(r"(?m)^(?=ERROR:|INFO:|Target )", console)
              if block.startswith("ERROR:") and re.search(missing_tmp, block, re.I)]
    failed = row["bep"]["failed_actions"]
    for action in failed:
        owner, mnemonic = action["owner"], action["mnemonic"]
        if mnemonic not in ("GoToolchainBinaryBuild", "GoStdlibList", "GoStdlib", "GoCompilePkg"):
            continue
        matches = [r for r in actions if r["owner"].lstrip("@") == owner and r["mnemonic"] == mnemonic
                   and r["fresh_remote"] and r["exit"]]
        if len(matches) != 1 or sum(a["owner"] == owner and a["mnemonic"] == mnemonic for a in failed) != 1:
            continue
        # Keep an attribution only when the diagnostic block identifies this
        # BEP action's exact primary output or explicit "(from target ...)".
        for header in errors:
            labels = re.findall(r"\(from target ([^)\s]+)\)", header)
            if re.search(r"\b" + mnemonic + r"\b", header) and (
                    owner in [label.lstrip("@") for label in labels] or action["output"] in header.split()):
                row["temp_failures"].append({**action, "action": matches[0]["hash"]})
                break
    for case in ("root", "bootstrap", "stdlib", "list", "package", "compiler", "stub"):
        try:
            row["checks"][case] = {"passed": True, **validate(row, actions, case)}
        except (AssertionError, OSError, ValueError, KeyError, subprocess.SubprocessError) as err:
            row["checks"][case] = {"passed": False, "error": type(err).__name__,
                                   "line": err.__traceback__.tb_next.tb_lineno if err.__traceback__.tb_next else None}
    # Do not export action commands, paths outside the requested owners, or logs.
    del row["bep"]["action_configs"]
    targets = row["bep"]["targets"]
    row["verified"] = (row["bep"].get("exit") == row["exit"] == 0 and set(targets) == set(TARGETS)
                       and all(t["built"] and not t.get("missing_file_set") for t in targets.values())
                       and all(c["passed"] for c in row["checks"].values()))
    row["stage"] = "read"


if __name__ == "__main__":
    assert platform.system() == "Linux" and platform.machine() == MACHINE
    report = {"carrier": os.environ["GITHUB_SHA"], "arch": ARCH, "image": IMAGE, "targets": TARGETS, "sources": {}}
    for source in (STOCK, FIXED):
        try:
            run_case(source, report)
        except (AssertionError, OSError, KeyError, ValueError, subprocess.SubprocessError) as err:
            report["sources"][source]["reader_error"] = type(err).__name__
        write(report)
    stock, fixed = (report["sources"][s] for s in (STOCK, FIXED))
    stock_observed = bool(stock.get("verified") or (stock.get("exit") and stock.get("checks", {}).get("root", {}).get("passed")
                          and stock.get("temp_failures")))
    report["stock_disposition"] = "passed" if stock.get("verified") else "observed_temp_failure" if stock_observed else "incomplete"
    report["verified"] = bool(stock_observed and fixed.get("verified"))
    write(report)
    sys.exit(0 if report["verified"] else 1)
