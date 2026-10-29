"""Compare the source archive, native Bazel outputs and disposable Go ref."""
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import re
import shlex

import qualification_report as reporter
import zipfile

def sha(data):
    return hashlib.sha256(data).hexdigest()

def git(*args):
    return subprocess.check_output(["git", *args])

def main():
    work = Path(os.environ["RUNNER_TEMP"])
    events = [json.loads(line) for line in (work / "native.bep").open()] if (work / "native.bep").exists() else []
    result = {"sha": git("rev-parse", "HEAD").decode().strip(), "machine": platform.machine(),
              "script_exit": int((work / "script.exit").read_text()),
              "bazel_exit": next((e["finished"]["exitCode"].get("code", 0) for e in events if "finished" in e), None)}
    output = work / "native-result.json"
    output.write_text(json.dumps(result, indent=2))
    assert result["sha"] == os.environ["PRODUCT"] and result["machine"] == "x86_64"
    assert result["script_exit"] == result["bazel_exit"] == 0
    owners = [e for e in events if e.get("id", {}).get("targetCompleted", {}).get("label") == "//:gopath"
              and not e["id"]["targetCompleted"].get("aspect") and e["completed"].get("success")]
    assert len(owners) == 1
    owner = reporter.Owner.parse(owners[0]["id"]["targetCompleted"])
    [advertised] = reporter.advertised_build_outputs(work / "native.bep", {(owner, "default")})
    archive = Path("bazel-bin/gopath.zip")
    body = archive.read_bytes()
    assert advertised["name"].endswith("/gopath.zip")
    assert advertised["sha256"] == sha(body) and advertised["bytes"] == len(body)
    result.update(zip=sha(body), size=len(body), go_ref=git("rev-parse", "refs/heads/go").decode().strip())
    parents = git("show", "-s", "--format=%P", "refs/heads/go").decode().split()
    assert len(parents) == 2 and parents[1] == result["sha"] and parents[0] == (work/"old-go").read_text().strip()
    reports, keyed_files, actions = {}, {}, []
    child = subprocess.Popen([str(work / "bb"), "print", "--raw=true", "--sort=false",
                              "--compact_execution_log=" + str(work / "native.binpb")], stdout=subprocess.PIPE)
    chunk = []
    for line in child.stdout:
        chunk.append(line)
        if line.rstrip(b"\r\n") != b"}":
            continue
        row = json.loads(b"".join(chunk))
        chunk.clear()
        if (f := row.get("file")) and ("/vdso/" in f["path"] or "/pkg/sentry/loader/vdsodata/" in f["path"] or
                                       "/pkg/sentry/platform/systrap/sysmsg/" in f["path"] or
                                       f["path"].endswith("/gopath.zip")):
            keyed_files[row["id"]] = f
        if (spawn := row.get("spawn")) and (spawn.get("targetLabel", "").startswith(("//pkg/sentry/loader/vdsodata:",
                "//pkg/sentry/platform/systrap/sysmsg:", "//vdso:")) or spawn.get("targetLabel") == "//:gopath"):
            actions.append(spawn)
    assert not chunk and child.wait() == 0
    for action in actions:
        for out in action.get("outputs", []):
            if f := keyed_files.get(out.get("outputId")):
                reports.setdefault(f["path"], []).append((action, f))
    direct = [a for a in actions if a.get("targetLabel") == "//:gopath" and any(
        keyed_files.get(o.get("outputId"),{}).get("digest",{}).get("hash") == result["zip"] for o in a.get("outputs",[]))]
    assert len(direct) == 1
    result["zip_action"] = {k: direct[0].get(k) for k in ("mnemonic", "runner", "cacheHit")}
    result["tools"] = [{"label":a["targetLabel"], "runner":a.get("runner"), "cache":a.get("cacheHit",False),
                       "declared_tools":sorted(set(re.findall(r"[A-Za-z0-9_/.+~-]+/(?:clang(?:\+\+)?|ld\.lld|llvm-(?:nm|objcopy))",
                                                           " ".join(a.get("args",[])))))}
                       for a in actions if a["mnemonic"] == "Genrule"]
    observed = [{"label":a.get("targetLabel"), "mnemonic":a["mnemonic"], "runner":a.get("runner"),
                 "cache":a.get("cacheHit",False), "files":[{"path":f["path"], "digest":f["digest"]} for o in a.get("outputs",[])
                            if (f := keyed_files.get(o.get("outputId"))) and "digest" in f]}
                for a in actions if a.get("mnemonic") in {"Genrule", "ArchGenruleCp"} or a.get("targetLabel") == "//:gopath"]
    (work / "native-actions.json").write_text(json.dumps(observed, indent=2))
    def generator(path, expected_label):
        [(action, file)] = [(a,f) for a,f in reports[path] if a.get("targetLabel") == expected_label
                            and a["mnemonic"] == "Genrule"]
        assert not action.get("cacheHit") and "sandbox" in action.get("runner", ""), (expected_label, action.get("runner"))
        return file
    outputs = {}
    with zipfile.ZipFile(archive) as zipped:
        for arch, machine in (("amd64",62), ("arm64",183)):
            for path in (f"pkg/sentry/loader/vdsodata/vdso_{arch}.so",
                         f"pkg/sentry/platform/systrap/sysmsg/sighandler.built-in.{arch}.bin",
                         f"pkg/sentry/platform/systrap/sysmsg/sighandler_{arch}.go"):
                value = zipped.read("src/gvisor.dev/gvisor/" + path)
                assert value and git("show", "refs/heads/go:" + path) == value
                [(action, f)] = [(a,f) for p, records in reports.items() if p.endswith("/bin/" + path)
                                    for a,f in records if a["mnemonic"] == "ArchGenruleCp"]
                tokenized = [s for arg in action.get("args", []) for s in shlex.split(arg)]
                [src] = [s for s in tokenized if s.startswith("bazel-out/") and s != f["path"] and s in reports]
                sysmsg = "//pkg/sentry/platform/systrap/sysmsg:"
                expected_label = "//vdso:vdso" if path.endswith(".so") else sysmsg + (
                    "sighandler_binary" if path.endswith(".bin") else "sighandler_go")
                produced = generator(src, expected_label)
                assert f["digest"] == produced["digest"]
                assert f["digest"]["hash"] == sha(value) and int(f["digest"]["sizeBytes"]) == len(value)
                info = {"sha256": sha(value), "size": len(value), "owner": action["targetLabel"],
                        "runner": action.get("runner"), "cache": action.get("cacheHit", False),
                        "source_action": expected_label, "generated": src}
                if not path.endswith(".so"):
                    linked = generator(src.rsplit("/",1)[0] + "/sighandler.built-in.bin.o", sysmsg + "sighandler_object")
                    info["linked_object"] = linked["digest"]
                if path.endswith(".so"):
                    assert value[:6] == b"\x7fELF\x02\x01" and int.from_bytes(value[16:18],"little") == 3
                    assert int.from_bytes(value[18:20],"little") == machine
                    info["elf_machine"] = machine
                outputs[path] = info
    assert len({v["sha256"] for v in outputs.values()}) == 6
    result["files"] = outputs
    result["entries"] = len(zipfile.ZipFile(archive).namelist())
    output.write_text(json.dumps(result, indent=2) + "\n")

if __name__ == "__main__":
    main()
