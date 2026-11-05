"""Qualify the two installed binaries and exercise the original TLDK wire test."""

from collections import Counter
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import traceback

import consumer
import native_cross_inputs as evidence

SOURCE = "cad667dcd75b15f549be5d4eeb23406433d7faa5"
RUNSC, SENTRY = consumer.RUNSC, consumer.SENTRY
FLAGS = ["--config=x86_64", "-c", "opt", "--jobs=4", "--remote_cache=", "--remote_executor=", "--disk_cache="]
DIR = Path(os.environ["RUNNER_TEMP"]) / "tldk-llvm"
R = {"status": "INCOMPLETE", "source": SOURCE, "carrier": os.environ["CARRIER"], "flags": FLAGS,
     "plugin_config": "--config=plugin-tldk", "expected": [RUNSC, SENTRY]}


def phase(name):
    R["phase"] = name
    save()


def save():
    (DIR / "receipt.json").write_text(json.dumps(R, indent=2) + "\n")


def command(*args, timeout=120):
    rc, out, err = evidence.command(list(map(str, args)), timeout)
    if rc:
        R["command_error"] = {"program": Path(args[0]).name, "exit": rc,
                              "diagnostics": evidence.diagnostics(err + b"\n" + out)}
        raise ValueError("command failed")
    return out


def compiler_markers(path):
    # GNU readelf walks archive members, including identically named objects.
    members = command("ar", "t", path).splitlines()
    listing = command("readelf", "-p", ".comment", path)
    counts = Counter()
    for part in re.split(rb"(?m)^File: [^\n]+\n", listing)[1:]:
        families = sorted({f for pat, f in ((b"clang version", "clang"), (b"GCC:", "gcc"))
                           if pat.lower() in part.lower()})
        counts["+".join(families) or "no_family_marker"] += 1
    return {"archive_sha256": evidence.digest(path.read_bytes()), "members": len(members),
            "comment_families": counts, "unlisted": len(members) - counts.total()}


def pcln_symbols(path, wanted):
    # Read names referenced by the live Go function table, not raw strings.
    # See runtime/symtab.go: pcHeader, functab and _func.nameOff.
    data = path.read_bytes()
    start = struct.unpack_from("<Q", data, 40)[0]
    width, count, names = struct.unpack_from("<HHH", data, 58)
    sections = [struct.unpack_from("<IIQQQQIIQQ", data, start + i * width) for i in range(count)]
    names = data[sections[names][4]:][:sections[names][5]]
    found = {}
    for s in sections:
        name = names[s[0]:].split(b"\0", 1)[0]
        if name not in (b".gopclntab", b".data.rel.ro.gopclntab", b".rodata", b".data.rel.ro"):
            continue
        layout = data[s[4]:s[4]+s[5]]
        prefix = b"\xf1\xff\xff\xff\0\0\x01\x08"
        pos = layout.find(prefix)
        while pos >= 0:
            if pos + 72 <= len(layout):
                n, _, _, fname, _, _, _, ftab = struct.unpack_from("<8Q", layout, pos+8)
                if 0 < n < 2000000 and 0 < fname < ftab and pos + ftab + 8*n <= len(layout):
                    for item in range(n):
                        _, off = struct.unpack_from("<II", layout, pos + ftab + 8*item)
                        entry = pos + ftab + off
                        if entry + 8 > len(layout):
                            continue
                        off = struct.unpack_from("<i", layout, entry + 4)[0]
                        if off < 0:
                            continue
                        first = pos + fname + off
                        stop = layout.find(b"\0", first, first + 512)
                        if stop != -1:
                            symbol = layout[first:stop]
                            for w in wanted:
                                if b"/plugin/cgo._Cfunc_" + w.encode() in symbol:
                                    found[w] = symbol.decode("ascii")
            pos = layout.find(prefix, pos + 1)
    return found


def binary(path, wanted):
    info = consumer.elf(path)
    rc, out, _ = evidence.command(["nm", "--defined-only", str(path)], 60)
    names = {line.rsplit(None, 1)[-1].decode(errors="replace") for line in out.splitlines() if line.strip()} if not rc else set()
    exact = [w for w in wanted if w in names]
    info["defined_c_symbols"] = exact
    if len(exact) != len(wanted):
        info["live_go_cgo_functions"] = pcln_symbols(path, set(wanted) - set(exact))
    info["linked_plugin_symbols_verified"] = set(exact) | set(info.get("live_go_cgo_functions", {})) == set(wanted)
    return info


def main():
    assert command("git","rev-parse","HEAD").decode().strip() == SOURCE
    assert command("bazel","--version").strip() == b"bazel 9.3.1"
    R["devices"] = consumer.devices()
    phase("build normal plugin targets")
    bep = DIR / "link.bep"
    rc, _, err = evidence.command(["bazel","build",*FLAGS,"--config=plugin-tldk","--keep_going",
        "--build_event_publish_all_actions","--build_event_json_file_path_conversion=true",
        "--build_event_json_file=" + str(bep), RUNSC,SENTRY], 4450)
    if rc:
        R["status"] = "BUILD_FAILED"
    R["build_exit"] = rc
    R["diagnostics"] = evidence.diagnostics(err)
    # Preserve the first actual compiler/linker failures even if preliminary
    # diagnostics or generic Bazel errors consumed the shared summary limit.
    source_lines = [line for line in err.splitlines() if re.search(rb"(?:ld.lld|clang(?:\+\+)?): (?:fatal )?error:|:[0-9]+:[0-9]+: (?:fatal )?error:|undefined reference to",line)]
    R["compiler_diagnostics"] = evidence.diagnostics(b"\n".join(source_lines))
    R["glibc_archives"] = {}
    if bep.is_file():
        with bep.open() as stream:
            for line in stream:
                ev = json.loads(line)
                key = ev.get("id",{}).get("actionCompleted",{})
                action = ev.get("action",{})
                output = Path(key.get("primaryOutput","")).name
                if "//3rd_party/libc/glibc/static:" in key.get("label","") and output in ("libc.a","libm.a","libpthread.a","libdl.a"):
                    R["glibc_archives"][output] = {"action":action.get("type"),"success":bool(action.get("success")),
                                                  "target":key["label"],"exit":action.get("exitCode",0)}
    save()
    evidence.ROOTS = [RUNSC,SENTRY]
    evidence.owned = lambda label: label in evidence.ROOTS or label.endswith("//:libpluginstack")
    events = evidence.events(bep) if bep.is_file() else {}
    R["events"] = events
    root = Path(command("bazel","info","execution_root").decode().strip())
    foreign = [a for a in events.get("actions",[]) if a["owner"].endswith("//:libpluginstack")
               and a["type"].endswith("MakeRule") and a["success"]]
    if foreign:
        # foreign_cc's primary output is its include directory. Query the same
        # dependency in the consuming configuration to find the archive.
        expression = 'filter("libpluginstack//:libpluginstack$", deps(set(' + RUNSC + ' ' + SENTRY + ')))'
        paths = command("bazel","cquery",*FLAGS,"--config=plugin-tldk","--output=starlark",
                        '--starlark:expr="\\n".join([f.path for f in target.files.to_list()])',expression)
        listed = {Path(p) for p in paths.decode().splitlines() if p.endswith("/libpluginstack.a")}
        R["compiler_evidence"] = {}
        for action in foreign:
            base = Path(action["output"]).parent
            matches = [p for p in listed if p.is_relative_to(base)]
            assert len(matches) == 1 and not matches[0].is_absolute() and ".." not in matches[0].parts
            R["compiler_evidence"][action["configuration"]] = compiler_markers(root / matches[0])
    save()
    if rc:
        R["status"] = "BUILD_FAILED"
        return
    assert {t["owner"] for t in events["targets"] if t["success"]} == {RUNSC,SENTRY}
    ev = consumer.events(bep)
    producers = {}
    for t in (RUNSC,SENTRY):
        producers[t], = consumer.outputs(ev,t).values()
    phase("make refresh with plugin binaries")
    install = DIR / "installed"
    command("make","DOCKER_BUILD=false","RUNTIME_DIR="+str(install),"BAZEL_OPTIONS="+" ".join(FLAGS),
        "RUNSC_TARGET=--config=plugin-tldk "+RUNSC,"EXTRA_SIDECAR_TARGETS=--config=plugin-tldk "+SENTRY,"refresh",timeout=1800)
    runsc = install / "runsc"
    sentry = install / "gvisor-bin/gvisor_sentry_plugin_stack"
    R["installed"] = {}
    for t,path,want in ((RUNSC,runsc,["plugin_preinitstack"]),(SENTRY,sentry,["plugin_initstack","plugin_socket"])):
        entry = R["installed"][t] = binary(path,want)
        assert entry["sha256"] == evidence.digest(producers[t].read_bytes())
        assert b"runsc version " in command(path,"--version")
        entry["version_startup"] = True
        save()
    table = json.loads(command(sentry,"syscalls","--format=json","--os=linux","--arch=amd64"))
    R["sentry_read_entry"] = table["linux"]["amd64"]["syscalls"]["0"]["name"]
    assert R["sentry_read_entry"] == "read"
    if all(v["opened"] for v in R["devices"].values()):
        phase("real external TCP and UDP")
        logs = DIR / "logs"
        logs.mkdir()
        command("sudo",runsc,"install","--runtime="+consumer.RUNTIME,"--download-sidecars=NEVER","--",
                "--network=plugin","--platform=systrap","--debug","--debug-log="+str(logs)+"/")
        command("sudo","systemctl","reload","docker")
        runtime = json.loads(command("sudo","cat","/etc/docker/daemon.json"))["runtimes"][consumer.RUNTIME]
        assert runtime["path"] == str(runsc) and {"--network=plugin","--platform=systrap"} <= set(runtime["runtimeArgs"])
        donor = "test/rbe/plugin_programs/"
        # Only the two original harness files enter the test checkout after the
        # binaries have been installed and checked against their product BEP.
        command("git","restore","--source="+R["carrier"],"--",donor+"BUILD",donor+"external_traffic.py")
        image = "python@sha256:69ebe56e2a764c399514a0a6117131cb730cc4d0e71e85c08deb9af74cbb9f41"
        command("docker","pull",image,timeout=180)
        image_id = command("docker","image","inspect","-f","{{.Id}}",image).decode().strip()
        consumer.test_case(R,"external",["bazel"],FLAGS,"//test/rbe/plugin_programs:external_traffic",
             "TestExternalTraffic",["--test_timeout=180","--test_env=REPORT_DIR="+str(DIR)],owners=("external",))
        markers = {"plugin_sentry": 'Sidecar "gvisor_sentry_plugin_stack" found: booting sandbox with '+str(sentry),
                   "plugin_init": "Initializing plugin network stack, config:"}
        R["runtime_markers"] = dict.fromkeys(markers,False)
        for path in logs.iterdir():
            content = command("sudo","tail","-c","4000000",path).decode(errors="replace")
            for k,v in markers.items(): R["runtime_markers"][k] |= v in content
        save()
        wire = json.loads((DIR / "wire.json").read_text())
        R["traffic"] = {k:{"passed":v.get("ok",False),"bridge_payloads":sorted(v.get("wire",{})),
                           "error": v.get("error"),"image_matches":v.get("image")==image_id} for k,v in wire.items()}
        assert set(wire) == {"runc",consumer.RUNTIME}
        assert all(v["passed"] and v["image_matches"] and len(v["bridge_payloads"])==4 for v in R["traffic"].values())
        assert all(R["runtime_markers"].values()) and R["external"]["status"] == "PASS"
    else:
        R["traffic"] = "not exercised: native TUN or vhost-net device unavailable"
    assert all(v["linked_plugin_symbols_verified"] for v in R["installed"].values())
    R["status"] = "PASS"
    phase("verified")


try:
    main()
except Exception as exc:
    R["error"] = {"type":type(exc).__name__,"lines":[x.lineno for x in traceback.extract_tb(exc.__traceback__)]}
finally:
    R["verdict_at"] = consumer.now()
    save()
raise SystemExit(0 if R["status"]=="PASS" else 1)
