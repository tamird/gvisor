"""Check opt-in selection, the actual DPDK stack dump, and the public plugin links."""
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import traceback
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

import native_cross_inputs as evidence


ROOT = Path(os.environ["RUNNER_TEMP"]) / "tldk-llvm"
SOURCE = "dd63198c66946d7a38279478369bf269243a6ef4"
TEST = "@libpluginstack//:backtrace_test"
ARCHIVE = "@libpluginstack//:libpluginstack"
TARGETS = {"//runsc:runsc-plugin-stack": ["plugin_preinitstack"],
           "//runsc/cmd/sentry:gvisor_sentry_plugin_stack": ["plugin_initstack", "plugin_socket"]}
BASE = ["--config=x86_64", "-c", "opt", "--strip=never", "--jobs=4", "--lockfile_mode=off",
        "--remote_cache=", "--remote_executor=", "--disk_cache="]
PLUGIN = ["--config=plugin-tldk"]
MUSL = ["--platforms=@llvm//platforms:linux_x86_64_musl",
        "--extra_toolchains=@llvm//toolchain:linux_x86_64_to_linux_x86_64"]


def bazel(command, flags, args, limit=330):
    return evidence.command([str(ROOT / "bazel"), command, *flags, *args], limit)


def label(text):
    return re.sub(r"^@@[^/]*[+]libpluginstack//", "@libpluginstack//", text)


def selection(r):
    checks = r["checks"] = {}
    for name, opt in (("default", []), ("gnu", PLUGIN), ("musl", PLUGIN + MUSL)):
        args = ["--output=starlark", '--starlark:expr=str(target.label)+" "+str("IncompatiblePlatformProvider" in providers(target))',
                f"set({TEST} {ARCHIVE})"]
        rc, data, err = bazel("cquery", BASE + opt, args)
        observed = {label(row.rsplit(" ", 1)[0]):row.rsplit(" ", 1)[1] for row in data.decode().splitlines() if " " in row}
        checks[name] = {"exit":rc, "incompatible":observed, "diagnostics":evidence.diagnostics(err)}
    for name, c in checks.items():
        assert c["exit"] == 0 and c["incompatible"] == dict.fromkeys((TEST, ARCHIVE), str(name == "default")), name
    assert TEST in Path("Makefile").read_text()
    r["status"] = "PASS"


def physical(path):
    assert path.is_file() and 0 < path.stat().st_size <= 300 * 1024**2
    data = path.read_bytes()
    assert data[:6] == b"\x7fELF\x02\x01" and int.from_bytes(data[18:20], "little") == 62
    program = subprocess.check_output(["readelf", "-lW", str(path)])
    dynamic = subprocess.check_output(["readelf", "-dW", str(path)])
    return {"sha256":hashlib.sha256(data).hexdigest(), "bytes":len(data), "machine":62,
            "interpreter":b"INTERP" in program, "needed":b"(NEEDED)" in dynamic}


def read_events(bep, row):
    row.update(failures=[], actions=[], targets={}, attempts=[])
    if not bep.is_file():
        return
    for line in bep.open():
        event = json.loads(line)
        key = event["id"]
        if "started" in event:
            row["invocation"] = event["started"]["uuid"]
        if "finished" in event:
            row["exit_name"] = event["finished"]["exitCode"]["name"]
        if "targetCompleted" in key and "completed" in event:
            k = key["targetCompleted"]
            if not k.get("aspect") and label(k["label"]) in (*TARGETS, TEST, ARCHIVE):
                row["targets"][label(k["label"])] = {"cfg":k["configuration"]["id"], "ok":event["completed"].get("success",False)}
        if "actionCompleted" in key and "action" in event:
            k, a = key["actionCompleted"], event["action"]
            owner, kind = label(k.get("label", "")), a.get("type", "")
            item = {"owner":owner, "type":kind, "exit":a.get("exitCode",0), "ok":a.get("success",False),
                    "cfg":k.get("configuration",{}).get("id"), "output":k.get("primaryOutput","")}
            if not item["ok"] and len(row["failures"]) < 8:
                row["failures"].append(item)
            if (owner in TARGETS or owner in (TEST,ARCHIVE)) and (kind.endswith("MakeRule") or kind == "GoLink"):
                row["actions"].append(item)
        if "testResult" in key and "testResult" in event:
            k, result = key["testResult"], event["testResult"]
            attempt = {"owner":label(k["label"]), "cfg":k["configuration"]["id"],
                       "id":[k.get(n) for n in ("run","shard","attempt")], "status":result["status"],
                       "cached":bool(result.get("cachedLocally") or result.get("executionInfo",{}).get("cachedRemotely")),
                       "strategy":result.get("executionInfo",{}).get("strategy")}
            row["attempts"].append(attempt)
            for output in result.get("testActionOutput", []):
                if output.get("name") != "test.log":
                    continue
                uri = urlsplit(output.get("uri", ""))
                if uri.scheme != "file" or uri.netloc not in ("", "localhost"):
                    continue
                log = Path(unquote(uri.path)).resolve(strict=True)
                if not log.is_relative_to(Path("bazel-out").resolve(strict=True)) or log.name != "test.log":
                    continue
                data = log.read_bytes()
                attempt["log"] = {
                    "sha256": evidence.digest(data), "bytes": len(data),
                    "case": [line[:200] for line in data.decode(errors="replace").splitlines()
                             if "TestDumpStack" in line or "stack trace contains" in line][:12],
                }
            matches = [f for f in result.get("testActionOutput",[]) if f.get("name") == "test.xml"]
            if len(matches) == 1:
                f, = matches
                uri = urlsplit(f.get("uri",""))
                assert uri.scheme == "file" and uri.netloc in ("","localhost")
                xml = Path(unquote(uri.path)).resolve(strict=True)
                assert xml.is_relative_to(Path("bazel-out").resolve(strict=True)) and xml.name == "test.xml" and xml.parent.name == "backtrace_test"
                assert 0 < xml.stat().st_size < 1024**2
                raw = xml.read_bytes()
                attempt["xml"] = {"sha256":evidence.digest(raw), "bytes":len(raw),
                    "cases":[[c.get("name"), "FAIL" if c.find("failure") is not None or c.find("error") is not None
                              else "SKIP" if c.find("skipped") is not None or c.get("status")=="notrun" else "PASS"]
                             for c in ET.fromstring(raw).iter("testcase")]}
                assert not f.get("digest") or f["digest"] == attempt["xml"]["sha256"]


def check_test(mode, r):
    flags = BASE + PLUGIN + (MUSL if mode == "musl" else [])
    bep = ROOT / (mode + ".bep")
    rc, _, err = bazel("test", flags, ["--keep_going", "--build_event_publish_all_actions",
        "--nobuild_event_json_file_path_conversion", "--build_event_json_file="+str(bep),
        "--nocache_test_results", "--flaky_test_attempts=1", "--runs_per_test=1",
        "--test_env=GO_TEST_WRAP_TESTV=1", "--test_arg=-test.v", "--test_filter=^TestDumpStack$", "--test_timeout=90", TEST], 2700)
    r.update(native_exit=rc, diagnostics=evidence.diagnostics(err))
    read_events(bep, r)
    attempt, = r["attempts"]
    assert attempt["owner"] == TEST and not attempt["cached"] and attempt["id"] == [1,1,1]
    assert attempt["strategy"] in ("linux-sandbox","processwrapper-sandbox","local","standalone")
    assert attempt["status"] == "PASSED" and attempt["xml"]["cases"] == [["TestDumpStack","PASS"]]
    r["named_test"] = "PASS"
    assert rc == 0 and r["exit_name"] == "SUCCESS" and r["targets"][TEST] == {"ok":True, "cfg":attempt["cfg"]}
    # Inspect the archive from this test's configured dependency, even if later profiles fail.
    exp = 'filter("libpluginstack//:libpluginstack$", deps(' + TEST + '))'
    qrc, out, qe = bazel("cquery",flags,["--output=files", exp])
    paths = [Path(x) for x in out.decode().splitlines() if x.endswith("/libpluginstack.a")]
    assert qrc == 0 and len(set(paths)) == 1, evidence.diagnostics(qe)
    p = paths[0]
    assert not p.is_absolute() and ".." not in p.parts and p.parts[0] == "bazel-out"
    raw = p.read_bytes()
    assert raw.startswith(b"!<arch>\n")
    symbols = subprocess.check_output(["nm","-A",str(p)],stderr=subprocess.DEVNULL)
    known=("rte_dump_stack","backtrace","backtrace_symbols","_Unwind_Backtrace","_Unwind_GetIPInfo")
    r["archive"] = {"sha256":evidence.digest(raw),"bytes":len(raw),"symbols":{s:bool(re.search(rb"\s"+s.encode()+rb"$",symbols,re.M)) for s in known}}
    expected = ("backtrace","backtrace_symbols") if mode == "gnu" else ("_Unwind_Backtrace","_Unwind_GetIPInfo")
    assert all(r["archive"]["symbols"][s] for s in ("rte_dump_stack",*expected))
    cc = "@bazel_tools//tools/cpp:current_cc_toolchain"
    expr = '[p.compiler_executable for p in providers(target).values() if hasattr(p, "compiler_executable")]'
    crc, paths, ce = bazel("cquery", flags, ["--output=starlark","--starlark:expr="+expr,cc])
    r["cc_query"] = {"exit":crc,"gnu_driver":bool(re.search(rb"(?:[a-z0-9_-]+-)?gcc(?:-[0-9]+)?[\x27\x22]",paths)),
                     "declared_llvm":b"llvm-toolchain-minimal-linux-amd64" in paths,"diagnostics":evidence.diagnostics(ce)}
    assert not crc and (r["cc_query"]["gnu_driver"] if mode == "gnu" else r["cc_query"]["declared_llvm"])
    r["status"]="PASS"


def check_links(r):
    bep = ROOT / "links.bep"
    rc, _, err = bazel("build", BASE+PLUGIN+MUSL, ["--keep_going","--build_event_publish_all_actions",
             "--build_event_json_file="+str(bep), *TARGETS], 3600)
    r.update(native_exit=rc,diagnostics=evidence.diagnostics(err))
    read_events(bep, r)
    items=r["binaries"]={}
    for owner, wanted in TARGETS.items():
        info=items[owner]={}
        matches = [a for a in r["actions"] if a["owner"]==owner and a["type"]=="GoLink" and a["ok"] and not a["exit"]]
        if len(matches)!=1:
            continue
        action,=matches
        path=Path(action["output"])
        assert not path.is_absolute() and path.parts[0]=="bazel-out" and ".." not in path.parts
        info.update(physical(path))
        info["config_join"]=r["targets"].get(owner)=={"ok":True,"cfg":action["cfg"]}
        nm=subprocess.run(["nm","--defined-only",str(path)],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL).stdout
        names={line.rsplit(None,1)[-1].decode(errors="replace") for line in nm.splitlines() if line.strip()}
        info["plugin_symbols"]={s:(s in names) for s in wanted}
        if not all(info["plugin_symbols"].values()):
            live=pcln_symbols(path,set(wanted)-names)
            info["live_cgo_symbols"]=live
            info["plugin_symbols"].update({s:True for s in live})
    assert rc==0 and r["exit_name"]=="SUCCESS" and not r["attempts"]
    assert all(i.get("config_join") and not i["interpreter"] and not i["needed"] and all(i["plugin_symbols"].values()) for i in items.values())
    r["status"]="PASS"
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



def main():
    mode, = sys.argv[1:]
    assert mode in ("selection", "gnu", "musl", "links")
    output = ROOT / (mode+".json")
    report = {"status":"INCOMPLETE","source":SOURCE,"phase":mode}
    try:
        assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==SOURCE
        assert not subprocess.check_output(["git","diff","--name-only","HEAD","--"])
        assert os.uname().machine=="x86_64" and Path(".bazelversion").read_text().strip()=="9.3.1"
        if mode=="selection":
            selection(report)
        elif mode=="links":
            check_links(report)
        else:
            check_test(mode,report)
    except (AssertionError,ValueError,KeyError,OSError,subprocess.SubprocessError,ET.ParseError) as exc:
        report["projection_error"]={"type":type(exc).__name__,"line":traceback.extract_tb(exc.__traceback__)[-1].lineno}
    finally:
        output.write_text(json.dumps(report,indent=2)+"\n")
    print(mode,report["status"])
    return report["status"]!="PASS"


if __name__=="__main__":
    sys.exit(main())
