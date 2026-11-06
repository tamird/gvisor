"""Verify the reviewed archive record and the component license policy."""
import urllib.request
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


BASE = "f51a205a8f4afbc5ab41aded070df74951b081ea"
PRODUCT = "65cd120ffdcd7beb55016f61e68ec93bba997e5c"
CATALOG = Path("tools/licensecheck/dependencies.yaml")
POLICY = Path("governance/licensing.yaml")
CHECKER = Path("tools/licensecheck/licensecheck.go")
TARGET = "//tools/licensecheck/main:licensecheck"
TMP = Path(os.environ["RUNNER_TEMP"]) / "catalog-compare"
NAME = r"[a-zA-Z0-9_./+@-]{1,180}"
ENTRY = re.compile(r"^- dependency: (" + NAME + r")\s*$", re.M)
CASES = {
    "missing_entry": rf"missing entry for ({NAME})",
    "duplicate_entry": rf"duplicate entry for ({NAME})",
    "stale_entry": rf"stale entry for ({NAME}), which is no longer a dependency",
    "audit_version": rf"({NAME}) was audited at .+, but is now .+",
    "audit_digest": rf"({NAME}) was audited with sha256 .+, but is now pinned to .+",
    "no_license": rf"({NAME}) has no license",
    "unknown_license": rf"({NAME}) has unknown license .+",
    "no_exception": rf"({NAME}) uses disallowed licenses .+ and has no exception in the policy",
    "unused_exception": rf"unnecessary exception for ({NAME}), whose licenses are all allowed",
    "orphan_exception": rf"exception for ({NAME}), which is not a dependency",
    "changed_exception": rf"exception for ({NAME}) lists licenses .+, but the dependency uses .+",
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def save(receipt):
    raw = json.dumps(receipt, indent=2).encode()
    assert len(raw) < 131072
    (TMP / "receipt.json").write_bytes(raw)


def signature(path):
    return {"git_blob": git("rev-parse", "HEAD:" + str(path)), "bytes": path.stat().st_size,
            "sha256": digest(path.read_bytes())}


def measure(source, name, args, path, timeout):
    catalog = path.read_text()
    names = ENTRY.findall(catalog)
    assert len(names) == len(set(names)) and len(names) > 0
    row = {"source": source, "catalog_entries": len(names), "input_catalog_sha256": digest(catalog.encode())}
    assert git("rev-parse", "HEAD") == source and not git("status", "--short", "-uno")
    console = TMP / (name + ".console")
    with console.open("wb") as out:
        proc = subprocess.run(["timeout", "-k", "15", str(timeout), *args], stdout=out, stderr=subprocess.STDOUT)
    raw = console.read_bytes()
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw.decode(errors="replace"))
    row.update(exit=proc.returncode, console_sha256=digest(raw), console_bytes=len(raw),
               head_after=git("rev-parse", "HEAD"), changed_tracked=git("diff","--name-only","HEAD").splitlines(),
               status="DEADLINE" if proc.returncode==124 else "COMMAND_FAILED" if proc.returncode else "INCOMPLETE")
    counts=re.findall(r"(?m)^all (\d+) dependencies have up-to-date license entries that conform to governance/licensing\.yaml$",text)
    problems=re.findall(r"(?m)^licensecheck: (\d+) problems; regenerate .+ with licensecheck --mode=fetch, or amend governance/licensing\.yaml$",text)
    if len(counts)==1 and not proc.returncode and not problems:
        row.update(status="PASS",dependency_count=int(counts[0]),count_source="checker stdout",problems=[])
    elif proc.returncode and len(problems)==1 and not counts and proc.returncode!=124:
        n=int(problems[0]); lines=text.splitlines()
        idx=next(i for i,line in enumerate(lines) if re.match(r"^licensecheck: \d+ problems;",line))
        assert 0<n<=len(lines[:idx]) and n<500
        rows=[]
        for line in lines[idx-n:idx]:
            issue={"category":"other","sha256":digest(line.encode())}
            for kind,pattern in CASES.items():
                m=re.fullmatch(pattern,line)
                if m:issue.update(category=kind,name=m[1]);break
            rows.append(issue)
        missing={x['name'] for x in rows if x['category']=='missing_entry'}
        stale={x['name'] for x in rows if x['category']=='stale_entry'}
        assert not missing.intersection(names) and stale<=set(names)
        row.update(status="CATALOG_REJECTED",dependency_count=len(set(names)-stale|missing),
                   count_source="catalog names minus verifier stale entries plus verifier missing entries",
                   problems=rows,reported_problems=n)
    else:
        allow=("cannot run make mod","No such file","no such package","download failed","Error downloading",
               "Unable to find package","error loading package","not available","connection refused","no matching toolchains",
               "module extension","duplicate dependency name","could not resolve","checksum","manifest unknown","exit status")
        row['hints']=[s for s in allow if s.lower() in text.lower()]
    return row



ARCHIVE="https://github.com/hermeticbuild/llvm-redist/releases/download/llvmorg-23.1.2-r1/llvm-project-23.1.2.src.tar.zst"
ARCHIVE_SHA="271da53a2feb0cb221a9276a14268a3b76ad802c7fc8fb1098291a43b06e4c56"
NOTICE="https://raw.githubusercontent.com/llvm/llvm-project/85ac560262434c9ccfc0c183ec22d4138ed647fb/LICENSE.TXT"
TERMS=["Apache-2.0 WITH LLVM-exception","BSD-3-Clause","CC0-1.0","NCSA","Spencer-94"]
NOTICES={
    "LICENSE.TXT": "8d85c1057d742e597985c7d4e6320b015a9139385cff4cbae06ffc0ebe89afee",
    "clang/LICENSE.TXT": "ebcd9bbf783a73d05c53ba4d586b8d5813dcdf3bbec50265860ccc885e606f47",
    "llvm/LICENSE.TXT": "8d85c1057d742e597985c7d4e6320b015a9139385cff4cbae06ffc0ebe89afee",
    "llvm/lib/Support/COPYRIGHT.regex": "0424e57d4303164dc59a8509c20dae0518b853692e5c2b0e98b11816fdbc97c7",
    "llvm/lib/Support/BLAKE3/LICENSE": "6a94bedb8b707ed97f6e310d0d015ab14e0683ffa0a612b02958581b9cc9fc0e"
}
BACKENDS={"AArch64","AMDGPU","ARM","BPF","NVPTX","RISCV","SystemZ","WebAssembly","X86"}
KEY="@@llvm+//extensions:llvm.bzl%llvm"

def actual(args, seconds, cwd=None):
    p=subprocess.run(["timeout","-k","8",str(seconds),*args],capture_output=True,cwd=cwd)
    row=dict(exit=p.returncode,stdout_sha256=digest(p.stdout),stderr_sha256=digest(p.stderr))
    if p.returncode:
        row["hints"]=[s for s in ("Error downloading","no license file","permission denied","unknown","deadline","no such package")
                      if s.lower() in (p.stdout+p.stderr).decode(errors="replace").lower()]
    return row,p.stdout,p.stderr

def main():
    r=dict(source=PRODUCT,carrier=os.environ["CARRIER_SHA"],status="INCOMPLETE",commands={})
    try:
        git("checkout","--quiet","--detach",PRODUCT)
        assert git("rev-parse","HEAD^")==BASE and not git("status","--short","-uno")
        assert git("diff",BASE,PRODUCT,"--","tools/licensecheck/licensecheck.go").count("@@")==6
        v=measure(PRODUCT,"candidate",["make","DOCKER_BUILD=false","license-check"],CATALOG,1320)
        r["full_verify"]=v
        save(r)
        cmds=r["commands"]
        def command(name,args,cap=300):
            cmds[name],out,_=actual(args,cap);save(r);assert cmds[name]["exit"]==0,name;return out
        graph=json.loads(command("graph",["make","-s","mod","OPTIONS=","TARGETS=graph --depth 1 --extension_info=all --output json"]))
        uses=[e for e in graph["extensionUsages"] if e["key"]==KEY]
        assert len(uses)==1 and uses[0]["used_repos"]==["llvm-project"]
        show=command("show_repo",["make","-s","mod","OPTIONS=","TARGETS=show_repo @llvm-project"])
        assert b"llvm_project_archive(" in show and ARCHIVE.encode() in show and ARCHIVE_SHA.encode() in show
        args=re.search(rb'(?m)^  targets = (\[[^\n]*\]),$',show)
        r["backends"]=json.loads(args[1]) if args else []
        r["extension_key"]=KEY;r["archive_sha256"]=ARCHIVE_SHA
        save(r)
        command("checker_build",["bazel","build","-c","opt",TARGET],600)
        fname=command("checker_output",["bazel","cquery","-c","opt","--output=files",TARGET]).decode().strip()
        assert fname.startswith("bazel-out/") and fname.endswith("/licensecheck") and "\n" not in fname
        binary=Path(fname).resolve(strict=True);assert binary.is_file()
        r["checker_sha256"]=digest(binary.read_bytes())
        d=TMP/"one-source";d.mkdir()
        # The stock CLI enumerates through make mod. Give its one-archive Fetch
        # the actual repository declaration, excluding unrelated network work.
        (d/"graph.json").write_text(json.dumps({"extensionUsages":uses}))
        (d/"repo.txt").write_bytes(show)
        (d/"Makefile").write_text("mod:\nifeq ($(TARGETS),graph --depth 1 --extension_info=all --output json)\n\t@cat graph.json\nelse ifeq ($(TARGETS),show_repo @llvm-project)\n\t@cat repo.txt\nelse\n\t@exit 2\nendif\n")
        (d/"go.mod").write_text("module fixture.test/one\n")
        entry=re.search(r"(?ms)^- dependency: llvm-project\n.*?(?=^- dependency:|\Z)",CATALOG.read_text())[0]
        assert "license_source:" not in entry
        header, _, policies = POLICY.read_text().partition("exceptions:\n")
        ex = re.search(r"(?ms)^  - dependency: llvm-project\n.*?(?=^  - dependency:|\Z)", policies)[0]
        (d/"policy.yaml").write_text(header + "exceptions:\n" + ex)
        one = d/"one.yaml"
        one.write_text(entry)

        def audit(mode):
            opts = ["--mode="+mode, "--yaml=one.yaml", "--go_mod=go.mod", "--policy=policy.yaml"]
            result, out, err = actual([str(binary), *opts], 150, d)
            return result, (out+err).decode(errors="replace")

        absent = "no license file in hermeticbuild/llvm-redist@llvmorg-23.1.2-r1"
        for index in (1, 2):
            result, output = audit("fetch")
            generated = re.search(r"(?ms)^- dependency: llvm-project\n.*?(?=^- dependency:|\Z)", one.read_text())[0]
            result.update(kept="kept existing entry" in output, missing_notice=absent in output,
                          exact_entry=generated==entry, terms=re.findall(r"(?m)^    - (.+)$", generated))
            r["fetch"+str(index)] = result
            save(r)

        valid, msg = audit("verify")
        valid["accepted_one"] = "all 1 dependencies have up-to-date license entries" in msg
        r["verify_one"] = valid
        changed = show.replace(ARCHIVE_SHA.encode(), b"0"*64)
        assert changed != show
        (d/"repo.txt").write_bytes(changed)
        pin, msg = audit("verify")
        pin["rejected_pin"] = "was audited with sha256" in msg and "0"*64 in msg
        r["changed_pin"] = pin
        (d/"repo.txt").write_bytes(show)
        one.write_text("".join(s for s in entry.splitlines(keepends=True) if s.strip() not in ("- BSD-3-Clause","- CC0-1.0","- Spencer-94")))
        partial, msg = audit("verify")
        partial["rejected_root_only"] = "exception for llvm-project lists licenses" in msg
        r["root_only"] = partial
        one.unlink()
        empty, msg = audit("fetch")
        empty["requires_entry"] = "cannot fetch licenses for llvm-project" in msg and absent in msg
        r["fresh_fetch"] = empty

        passed = v["status"]=="PASS" and v["dependency_count"]==v["catalog_entries"]
        passed &= all(x["exit"]==0 and x["kept"] and x["missing_notice"] and x["exact_entry"] and x["terms"]==TERMS for x in (r["fetch1"],r["fetch2"]))
        passed &= valid["exit"]==0 and valid["accepted_one"]
        passed &= pin["exit"]!=0 and pin["rejected_pin"] and partial["exit"]!=0 and partial["rejected_root_only"]
        passed &= empty["exit"]!=0 and empty["requires_entry"] and not git("status","--short","-uno")
        r["status"] = "COMPONENT_AUDIT_VERIFIED" if passed else "INCOMPLETE"
    except (OSError,ValueError,KeyError,IndexError,TypeError,AssertionError,subprocess.CalledProcessError) as e:
        r["error"]=type(e).__name__
        r["reader_line"]=e.__traceback__.tb_lineno
    save(r)
    raise SystemExit(r["status"]!="COMPONENT_AUDIT_VERIFIED")

if __name__=="__main__":
    main()
