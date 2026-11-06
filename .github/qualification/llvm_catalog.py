"""Compare the project's unchanged dependency catalog in native verify mode."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


BASE = "f51a205a8f4afbc5ab41aded070df74951b081ea"
PRODUCT = "fcd2cc824cf0523364c9e9bab534fc8b5f70e2ab"
CATALOG = Path("tools/licensecheck/dependencies.yaml")
POLICY = Path("governance/licensing.yaml")
CHECKER = Path("tools/licensecheck/licensecheck.go")
TARGET = "//tools/licensecheck/main:licensecheck"
REMOVED = "llvm"
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


def main():
    report=dict(run=int(os.environ["QUALIFICATION_RUN_ID"]),carrier=os.environ["CARRIER_SHA"],
                status="INCOMPLETE",catalog_scope="project verify mode; does not audit all toolchain binaries",phases={})
    save(report)
    try:
        same=git("diff","--name-only",BASE,PRODUCT,"--",str(CATALOG),str(POLICY),"go.mod","Makefile","tools/licensecheck")
        assert same==str(CHECKER)
        a=git("show",BASE+":"+str(CHECKER))
        b=git("show",PRODUCT+":"+str(CHECKER))
        old="// Toolchain extensions (crosstool, llvm_zlib, python, go_sdk,\n\t\t\t// ...) are not audited."
        new="// Toolchain extensions (llvm_zlib, python, go_sdk, ...) are not audited."
        assert a.count(old)==1 and a.replace(old,new)==b
        report["checker_change"]="excluded-toolchain comment only"
        for name,source in (("upstream",BASE),("compiler",PRODUCT)):
            try:
                git("checkout","--quiet","--detach",source)
                input_files={str(p):signature(p) for p in (CATALOG,POLICY,CHECKER,Path("go.mod"))}
                r=measure(source,name,["make","DOCKER_BUILD=false","license-check"],CATALOG,1320)
                r["files"]=input_files
                report["phases"][name]=r
            except (OSError, ValueError, AssertionError, subprocess.CalledProcessError) as ex:
                report["phases"][name]={"status":"INCOMPLETE","error_type":type(ex).__name__}
            save(report)
        base,product=report["phases"]["upstream"],report["phases"]["compiler"]
        if product["status"] in ("PASS","CATALOG_REJECTED") and not product.get("changed_tracked"):
            entries=CATALOG.read_text()
            pattern=r"(?ms)^- dependency: "+re.escape(REMOVED)+r"\n.*?(?=^- dependency: |\Z)"
            reduced,num=re.subn(pattern,"",entries); assert num==1
            copy=TMP/"missing-llvm.yaml";copy.write_text(reduced)
            assert set(ENTRY.findall(reduced))==set(ENTRY.findall(entries))-{REMOVED}
            bad=measure(PRODUCT,"missing",["make","DOCKER_BUILD=false","run","TARGETS="+TARGET,
                "ARGS=--mode=verify --yaml="+str(copy)],copy,360)
            baseline=Counter(x["sha256"] for x in product.get("problems",[]))
            expected=baseline+Counter([digest(("missing entry for "+REMOVED).encode())])
            bad["exact_missing_control"]=bool(bad["status"]=="CATALOG_REJECTED" and Counter(x["sha256"] for x in bad["problems"])==expected and bad["dependency_count"]==product["dependency_count"])
            report["phases"]["missing_control"]=bad
            save(report)
        report["inputs_identical"]=all(base.get("files",{}).get(str(p))==product.get("files",{}).get(str(p)) and str(p) in base.get("files",{}) for p in (CATALOG,POLICY,Path("go.mod")))
        report["same_count"]=base.get("dependency_count") is not None and base.get("dependency_count")==product.get("dependency_count")
        report["same_problems"]=base.get("problems") is not None and base.get("problems")==product.get("problems")
        clean=all(not row.get("changed_tracked",["unreported"]) and row["source"]==row["head_after"] for row in report["phases"].values())
        if clean and report["inputs_identical"] and base.get("dependency_count") and product.get("dependency_count") and report["phases"].get("missing_control",{}).get("exact_missing_control"):
            report["status"]="CATALOG_DIFFERENCE" if not report["same_count"] or not report["same_problems"] else "INCOMPLETE"
            if report["same_count"] and base["status"]==product["status"]=="PASS":report["status"]="PASS"
            elif report["same_count"] and base["status"]==product["status"]=="CATALOG_REJECTED" and report["same_problems"]:report["status"]="BOTH_CATALOGS_REJECTED"
    except (AssertionError,OSError,ValueError,KeyError,subprocess.CalledProcessError) as ex:
        report["error_type"]=type(ex).__name__
    save(report)
    raise SystemExit(report["status"]!="PASS")


if __name__=="__main__":
    main()
