"""Bind one ARM64 runtime release to two original TCG test results."""

import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

SOURCE = "2aaaeb4b257fd9d957676a8c1b3da667dc800aec"
HARNESS = "ec4ced798422d27e533369b02d41b06f21b798c1"
ORIGINAL_PROBE = "c0a4df1bedf8e4e38033bf24da6b287fa2d429bfa932edb2d7c2e343d65e251a"
ORIGINAL_BLOB = "d67bbbc2e4dc8ce9eb96f3bd0410887062bbc1cea5708dcf4b0008f45d5b7d12"
WORK = Path(os.environ["RUNNER_TEMP"]) / "paired-stub-guest"
PKG = "test/rbe/paired_stub_guest"
ROOTS = ["//:release", "//pkg/sentry/platform/systrap/sysmsg:sighandler_binary"]
MEMBERS = {"runsc", "containerd-shim-runsc-v1"} | {"gvisor-bin/" + n for n in (
    "gvisor_sentry", "runsc-netgofer", "runsc-fd-parking", "gvisor-sentry-prewarmer", "gvisor-proc-umounter",
    "runsc-metric-server", "checkpointgofer")}
GUESTS = {"page4k": ("7.3.0-070300rc3-generic", 4096), "page64k": ("6.8.0-138-generic-64k", 65536)}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(name, value):
    (WORK / (name + ".json")).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def file_bytes(item, fallback=None):
    uri = urlparse(item["uri"])
    if uri.scheme == "file":
        assert not uri.netloc
        path = Path(unquote(uri.path))
    else:
        assert uri.scheme == "bytestream", uri.scheme
        path = fallback or Path(*item.get("pathPrefix", []), item["name"])
    assert path.stat().st_size <= 128 << 20, path.name
    data = path.read_bytes()
    digest = sha(data)
    if item.get("digest"):
        assert digest == item["digest"]
    if "length" in item:
        assert len(data) == int(item["length"])
    if uri.scheme == "bytestream":
        assert uri.path.endswith("/blobs/" + digest + "/" + str(len(data)))
    return path, data


def outputs(group, filesets):
    result = []
    seen = set()
    pending = [item["id"] for item in group["fileSets"]]
    while pending:
        key = pending.pop()
        if key not in seen:
            seen.add(key)
            row = filesets[key]
            result.extend(row.get("files", []))
            pending.extend(item["id"] for item in row.get("fileSets", []))
    return result


STUB = "//pkg/sentry/platform/systrap/sysmsg:sighandler_object"
BAZEL = [WORK / "bazel9", "--nosystem_rc", "--nohome_rc", "--bazelrc=" + str(WORK / "compiler.rc")]
BUILD_FLAGS = ["--config=aarch64", "-c", "opt", "--nostamp", "--jobs=80", "--lockfile_mode=off",
               "--distdir=" + str(WORK / "dist"), "--color=no", "--curses=no", "--noshow_progress"]


def compiler_evidence():
    sources = {"sighandler_arm64.c", "sysmsg_lib.c", "sigrestorer_arm64.S", "syshandler_arm64.S"}
    expression = 'inputs("pkg/sentry/platform/systrap/sysmsg/[^/]+[.][cS]$", mnemonic("CppCompile", deps(' + STUB + ')))'
    with (WORK / "stub-aquery.json").open("w") as output:
        subprocess.run([*BAZEL, "aquery", *BUILD_FLAGS, "--output=jsonproto", "--include_param_files",
                        expression], stdout=output, check=True)
    graph = json.loads((WORK / "stub-aquery.json").read_text())
    owners = {str(t["id"]): t["label"] for t in graph["targets"]}
    rows = {}
    for action in graph["actions"]:
        argv = list(action["arguments"])
        argv.extend(a for param in action.get("paramFiles", []) for a in param.get("arguments", []))
        compiled = [Path(arg).name for arg in argv if Path(arg).name in sources]
        if not compiled:
            continue
        source, = set(compiled)
        assert source not in rows, source
        targets = [arg.split("=", 1)[1] for arg in argv if arg.startswith(("--target=", "-target="))]
        targets.extend(value for flag, value in zip(argv, argv[1:]) if flag in ("-target", "--target"))
        defines = [arg for i, arg in enumerate(argv) if re.match(r"^-DPAGE_SIZE(?:=|$)", arg)
                   or (i > 0 and argv[i - 1] == "-D" and re.match(r"^PAGE_SIZE(?:=|$)", arg))]
        rows[source] = {"driver": argv[0], "target": targets, "PAGE_SIZE_defines": defines,
                        "owner": owners[str(action["targetId"])], "actionKey": action["actionKey"], "arguments": argv}
    save("compiler", rows)
    assert set(rows) == sources, rows
    for row in rows.values():
        assert "clang" in Path(row["driver"]).name and row["target"], row
        assert all(t.startswith("aarch64") for t in row["target"]) and not row["PAGE_SIZE_defines"], row
    return rows


def build():
    assert subprocess.check_output(["git", "rev-parse", "HEAD^"], text=True).strip() == SOURCE
    bep = WORK / "build.bep"
    command = [*BAZEL, "build", *BUILD_FLAGS, "--keep_going", "--build_event_json_file=" + str(bep), *ROOTS]
    with (WORK / "build.log").open("w") as log:
        rc = subprocess.run(command, stdout=log, stderr=log).returncode
    summary = {"source": SOURCE, "harness": HARNESS, "exit": rc, "members": {}, "roots": {},
               "original_blob_sha256": ORIGINAL_BLOB}
    save("release", summary)
    sets, targets = {}, {}
    for line in bep.open():
        e = json.loads(line)
        ident = e["id"]
        if "started" in e:
            summary["invocation"] = e["started"].get("uuid")
        if "namedSetOfFiles" in e:
            sets[ident["namedSet"]["id"]] = e["namedSetOfFiles"]
        target = ident.get("targetCompleted", {})
        if target.get("label") in ROOTS and not target.get("aspect"):
            label = target["label"]
            summary["roots"][label] = {"success": e.get("completed", {}).get("success", False),
                                       "config": target.get("configuration", {}).get("id")}
            if summary["roots"][label]["success"]:
                group, = [g for g in e["completed"]["outputGroup"] if g["name"] == "default"]
                targets[label] = group
        assert "testResult" not in e
    if rc:
        summary["diagnostics"] = diagnostics((WORK / "build.log").read_bytes())
    save("release", summary)
    inputs = WORK / "inputs"
    inputs.mkdir()
    if ROOTS[1] in targets:
        blob_item, = outputs(targets[ROOTS[1]], sets)
        _, blob = file_bytes(blob_item)
        assert blob
        summary["actual_blob_sha256"] = sha(blob)
        summary["same_as_original_blob"] = sha(blob) == ORIGINAL_BLOB
        summary["compilation"] = "Bazel --config=aarch64 -c opt"
        save("release", summary)
    if ROOTS[0] in targets:
        for item in outputs(targets[ROOTS[0]], sets):
            path, data = file_bytes(item)
            assert data[:6] == b"\x7fELF\x02\x01" and int.from_bytes(data[18:20], "little") == 183
            name = ("gvisor-bin/" if path.parent.name == "gvisor-bin" else "") + path.name
            assert name in MEMBERS and name not in summary["members"]
            summary["members"][name] = {"sha256": sha(data), "bytes": len(data)}
            if name == "gvisor-bin/gvisor_sentry" and ROOTS[1] in targets:
                summary["blob_copies_in_sentry"] = data.count(blob)
            target = inputs / "release" / name
            target.parent.mkdir(exist_ok=True, parents=True)
            shutil.copyfile(path, target)
            target.chmod(0o755)
    save("release", summary)
    assert rc == 0 and set(summary["members"]) == MEMBERS and summary["blob_copies_in_sentry"] == 1
    summary["release_verified"] = True
    save("release", summary)


def install_probe():
    summary = json.loads((WORK / "release.json").read_bytes())
    assert summary["release_verified"]
    inputs = WORK / "inputs"
    probe = Path("bazel-bin", PKG, "pagesize_probe")
    target = "//" + PKG + ":pagesize_probe"
    flags = [*BUILD_FLAGS, "--platforms=@llvm//platforms:linux_aarch64_musl"]
    query = 'mnemonic("Cpp(Compile|Link)", ' + target + ')'
    graph = json.loads(subprocess.check_output(
        [*BAZEL, "aquery", *flags, "--output=jsonproto", "--include_param_files", query]))
    commands = {}
    for action in graph["actions"]:
        argv = list(action["arguments"])
        for param in action.get("paramFiles", []):
            argv.extend(param.get("arguments", []))
        commands[action["mnemonic"]] = argv
    save("probe-toolchain", commands)
    # The ELF proves the target machine. The configured compile action
    # distinguishes Clang with the musl sysroot from the old GNU toolchain.
    subprocess.run([*BAZEL, "build", *flags, target], check=True)
    data = probe.read_bytes()
    summary["probe_sha256"] = sha(data)
    summary["probe_platform"] = "@llvm//platforms:linux_aarch64_musl"
    summary["probe_program_headers"] = subprocess.check_output(["readelf", "-lW", probe], text=True)
    summary["probe_dynamic"] = subprocess.check_output(["readelf", "-dW", probe], text=True)
    save("release", summary)
    assert data[:6] == b"\x7fELF\x02\x01" and int.from_bytes(data[18:20], "little") == 183
    assert "INTERP" not in summary["probe_program_headers"] and "(NEEDED)" not in summary["probe_dynamic"]
    assert {"CppCompile", "CppLink"} <= commands.keys()
    assert any("clang" in arg for arg in commands["CppCompile"])
    assert any("musl" in arg for arg in commands["CppCompile"])
    shutil.copyfile(probe, inputs / "probe")
    (inputs / "probe").chmod(0o755)
    manifest = inputs / "manifest.sha256"
    manifest.write_text("".join(f"{sha(p.read_bytes())}  {p.relative_to(inputs)}\n"
                                for p in sorted(inputs.rglob("*")) if p.is_file()))
    summary["manifest_sha256"] = sha(manifest.read_bytes())
    # Substituting the original probe digest must reproduce the historical
    # manifest for all nine shipped executables and the probe.
    current = summary["probe_sha256"] + "  probe\n"
    assert manifest.read_text().count(current) == 1
    original = manifest.read_text().replace(current, ORIGINAL_PROBE + "  probe\n")
    assert sha(original.encode()) == "72cfbcf5a3258453428aada4ff0305185669aca99e560f818184fc8237832fd1"
    summary["verified"] = True
    save("release", summary)


def diagnostics(data):
    lines = data.decode(errors="replace").splitlines()
    return [line[:650] for line in lines if re.search(
        r"(?i)(error:|ld[.]lld:|undefined reference|undefined symbol|cannot find|cannot open|referenced by|duplicate symbol)",
        line) and not re.search(r"(?i)(authorization|remote_header|bes_header|buildbuddy-api-key)", line)][:20]


def original_probe_diagnostic():
    # Read the immutable original build log from the same remote backend as Bazel.
    # Only write linker diagnostics to the public receipt.
    original = "2e3a5710-145d-4b8d-a1ff-6aa989993e20"
    invocation = original.encode()
    request = bytes([18, len(invocation)]) + invocation + bytes([32, 208, 15, 40, 1])
    conn = http.client.HTTPSConnection("remote.buildbuddy.io", timeout=15)
    found = []
    status = None
    try:
        headers = {"Content-Type": "application/proto", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]}
        conn.request("POST", "/rpc/BuildBuddyService/GetEventLogChunk", body=request, headers=headers)
        response = conn.getresponse()
        status = response.status
        if status == 200:
            found = diagnostics(response.read(4 << 20))
    except OSError:
        pass
    finally:
        conn.close()
    record = {"originalInvocation": original, "originalStatus": status, "original": found}
    save("probe-link", record)
    if any("ld.lld: error:" in line for line in found):
        return
    # Replay only the original independent probe, with the same inputs and flags.
    env = {k: os.environ[k] for k in ("HOME", "USER", "PATH", "RUNNER_TEMP")}
    result = subprocess.run([*BAZEL, "build", *BUILD_FLAGS, "//" + PKG + ":pagesize_probe"],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    record.update(replayExit=result.returncode, replay=diagnostics(result.stdout))
    save("probe-link", record)


def collect():
    release = json.loads((WORK / "release.json").read_bytes())
    assert release["verified"] and subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == HARNESS
    bep = WORK / "guests.bep"
    report = {"source": SOURCE, "harness": HARNESS, "manifest_sha256": release["manifest_sha256"],
              "original_invocation_exit": int((WORK / "remote.exit").read_text()), "results": {}, "errors": []}
    save("guests", report)
    for line in bep.open():
        e = json.loads(line)
        ident = e["id"]
        if "started" in e:
            report["invocation"] = e["started"].get("uuid")
        action = ident.get("actionCompleted", {})
        if "action" in e and not e["action"].get("success") and len(report["errors"]) < 7:
            report["errors"].append({"owner": action.get("label"), "mnemonic": e["action"].get("type"),
                                     "exit": e["action"].get("exitCode")})
        if "testResult" not in e:
            continue
        label = ident["testResult"]["label"]
        name = label.split(":")[-1]
        assert label == "//" + PKG + ":" + name and name in GUESTS and name not in report["results"]
        t = e["testResult"]
        info = t.get("executionInfo", {})
        row = {"status": t["status"], "cached": bool(t.get("cachedLocally") or info.get("cachedRemotely")),
               "strategy": info.get("strategy"), "duration_ms": t.get("testAttemptDurationMillis"), "label": label}
        report["results"][name] = row
        save("guests", report)
        for item in t["testActionOutput"]:
            if item["name"] not in ("test.log", "test.xml"):
                continue
            _, data = file_bytes(item, Path("bazel-testlogs", PKG, name, item["name"]))
            (WORK / (name + "-" + item["name"])).write_bytes(data)
            row[item["name"] + "_sha256"] = sha(data)
            if item["name"] == "test.xml":
                xml = ET.fromstring(data)
                cases = list(xml.iter("testcase"))
                row["xml_cases"] = len(cases)
                row["xml_pass"] = (bool(cases) and not any(
                    node.tag in ("failure", "error", "skipped")
                    or any(int(node.get(k, 0)) for k in ("failures", "errors", "skipped"))
                    or node.get("status") in ("notrun", "skipped") or node.get("result") in ("skipped", "suppressed")
                    for node in xml.iter()))
            else:
                log = data.decode(errors="replace").replace("\r", "")
                row["inputs"] = re.findall(r"PAIRED_INPUTS manifest=([0-9a-f]{64}) arch=(\w+) kernel=([\w.-]+) pages=(\d+)", log)
                row["probe"] = re.findall(r"SYSTRAP_PROBE arch=(\w+) auxv=(\d+) sysconf=(\d+) pid=(\d+)", log)
                row["sentries"] = re.findall(r"PAIRED_SENTRY sha256=([0-9a-f]{64})", log)
                row["exit"] = re.findall(r"PAIRED_EXIT status=(\d+)", log)
                row["guest_pages"] = re.findall(r"Guest page size: (\d+)", log)
                row["signals"] = [s for s in ("mount", "permission denied", "timed out", "No space left on device",
                                  "exec format error", "unexpected EOF") if s in log and t["status"] != "PASSED"]
        kernel, pages = GUESTS[name]
        row["verified"] = (row["status"] == "PASSED" and not row["cached"] and row["strategy"] == "remote"
                           and row.get("inputs") == [(release["manifest_sha256"], "aarch64", kernel, str(pages))]
                           and row.get("probe") == [("aarch64", str(pages), str(pages), "1")]
                           and row.get("sentries") == [release["members"]["gvisor-bin/gvisor_sentry"]["sha256"]]
                           and row.get("exit") == ["0"] and str(pages) in row.get("guest_pages", [])
                           and row.get("xml_pass") is True)
        save("guests", report)
    report["verified"] = (report["original_invocation_exit"] == 0 and set(report["results"]) == set(GUESTS)
                          and all(r["verified"] for r in report["results"].values()))
    if not report["verified"]:
        log = (WORK / "remote.log").read_text(errors="replace")
        # Preserve the first terminal diagnostic separately from repetitive download warnings.
        lines = [line for line in log.splitlines() if re.match(r"^(?:ERROR:|Error in | *Error:)", line)]
        report["failure_lines"] = []
        for line in lines:
            if any(s in line.lower() for s in ("header", "credential", "authorization", "token=", "signature=")):
                continue
            line = re.sub(r"https?://[^\s]+", lambda m: m.group().split("?", 1)[0], line)
            report["failure_lines"].append(line[:1000])
            if len(report["failure_lines"]) == 12:
                break
        report["diagnostic_classes"] = [s for s in ("no matching execution platform", "no registered executors",
            "no such package", "no such target", "not visible", "could not resolve", "not found", "timed out",
            "does not exist", "repository mapping", "status unavailable", "disk full") if s in log.lower()]
    save("guests", report)
    return not report["verified"]


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["release"]:
        build()
    elif args == ["probe"]:
        install_probe()
    elif args == ["diagnostic"]:
        original_probe_diagnostic()
    elif args == ["compiler"]:
        compiler_evidence()
    elif args == ["collect"]:
        raise SystemExit(collect())
    else:
        raise SystemExit("expected release, compiler or collect")
