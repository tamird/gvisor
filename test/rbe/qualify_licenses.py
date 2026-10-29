"""Run real license owners on exact source and publish only typed, bounded results."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

import qualification_report as reporter

OWNER = "//tools/licensecheck:licensecheck_test"
MAIN = "//tools/licensecheck/main:licensecheck"
PARENT = "9e3e72d7316c7530a4455f1d4ebb41f60ca064e3"
SOURCES = {
    "15114": ("e9289d0a24c17e33d9dccf880ddef2f7e7bf886c", "28fe70d2e4739ef976db2bd32f8dfd3ee224f18c"),
    "16051": ("dbf12f4d327472f76a83423c08ee14bbe7ec7058", "5bd049ee883607958bcf2e600ff23b7ed5b91723"),
    "16249": ("f8b7988ba059b9e88edda1264a0ad4907463e48e", "ebb9ec1ce841c002d691bfb33629a6b311ab26b1"),
    "16589": ("fb7bb85aefa9f128224ff9f536f95b856f99f621", "de87052942c2e3e119bd9ea0c7b8f79b077432d1"),
}
ID = re.compile(r"https://app\.buildbuddy\.io/invocation/([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})(?![0-9a-z-])")
OK = re.compile(r"^all (\d+) dependencies have up-to-date license entries that conform to governance/licensing\.yaml$", re.M)
HINT = ("GoToolchainBinaryBuild", "no such target", "unknown repository", "Cannot dump repository mapping", "does not exist", "not found", "no matching execution platform", "missing entry", "stale entry", "duplicate entry", "re-audit", "BUILD_FAILURE", "No such file or directory", "unrecognized option", "error loading", "permission denied", "unrecognized package")
def git(*args):
    return subprocess.check_output(["git",*args])

def dump(path, obj):
    path.write_text(json.dumps(obj, sort_keys=True, indent=2) + "\n")

def record(path):
    return reporter.file_record(path)

def processes(args, d):
    with (d/"stdout.log").open("wb") as out, (d/"stderr.log").open("wb") as err:
        cp = subprocess.run(args, stdout=out, stderr=err, check=False)
    return cp.returncode

def safe_name(text):
    return text if re.fullmatch(r"[A-Za-z0-9@_:/+., -]{1,200}",text) else "sha256:"+hashlib.sha256(text.encode()).hexdigest()

def phase(name, args, base, sha, rc):
    d=base/name; d.mkdir()
    status={"phase":name, "expected_owner":OWNER if name=="unit" else MAIN, "status":"INCOMPLETE","native_exit":None,"collector_exit":None}
    try:
        (d/"commit").write_bytes(git("cat-file","commit",sha))
        (d/"before").write_bytes(git("status","--porcelain","--untracked-files=no"))
        raw=d/"events.raw"
        if name=="unit":
            args=args[:-1]+["--build_event_json_file="+str(raw),"--build_event_json_file_path_conversion=false"]+args[-1:]
        n=processes(args,d)
        status["native_exit"]=n
        finished=datetime.now(timezone.utc).isoformat()
        (d/"exit").write_text(f"{n}\n")
        (d/"after").write_bytes(git("status","--porcelain","--untracked-files=no"))
        (d/"head").write_bytes(git("rev-parse","HEAD"))
        native_context={"expected_source":sha,"command_kind":"test" if name=="unit" else "command","execution_finished_at":finished,
              "commit_file":"commit","source_before":"before","source_after":"after","final_head":"head","native_exit":"exit","capture_exit":"capture","executed_arguments":"args"}
        dump(d/"context",native_context)
        source_ok=(not (d/"before").read_bytes() and not (d/"after").read_bytes() and (d/"head").read_text().strip()==sha)
        log='\n'.join((d/f).read_text(errors="replace") for f in ["stdout.log","stderr.log"])
        status.update({"source_clean":source_ok,"finish":finished,"invocations":list(dict.fromkeys(ID.findall(log))),"hints":[s for s in HINT if s.lower() in log.lower()],"logs":{f:record(d/f) for f in ["stdout.log","stderr.log"]}})
        if name=="unit":
            (d/"args").write_text('\n'.join(args[1:])+'\n')
            (d/"expected").write_text(OWNER+"\n")
            outroot=Path(subprocess.check_output(["bazel",f"--bazelrc={rc}","info","output_base"],stderr=subprocess.DEVNULL,text=True).strip())
            clean=d/"events.jsonl"
            meta=reporter.capture(raw,clean,outroot,d/"reports"); capture_exit=int(bool(meta["errors"]))
            (d/"capture").write_text(f"{capture_exit}\n")
            v=reporter.verdict(clean,reporter.manifest_path(clean),d/"context",d/"expected")
            observations=[json.loads(l) for l in clean.read_text().splitlines()]
            attempts=[e for e in observations if "testResult" in e]
            execution=[{"strategy":"remote" if e["testResult"].get("executionInfo",{}).get("strategy")=="remote" else "unknown","cached":bool(e["testResult"].get("cachedLocally") or e["testResult"].get("executionInfo",{}).get("cachedRemotely")), "owner":OWNER if e["id"]["testResult"]["label"]==OWNER else "unexpected"} for e in attempts]
            records=[r for r in meta["reports"] if r.get("name") in ("test.xml","test.log")]
            xmls=[r for r in records if r["name"]=="test.xml"]
            nonpass=[]; case_names=[]
            for xml in xmls:
                for tc in ET.parse(d/xml["path"]).iter("testcase"):
                    case_names.append(tc.get("name",""))
                    how=next((x.tag for x in tc if x.tag in ("failure","error","skipped")),None)
                    if how: nonpass.append([how,safe_name(tc.get("name",""))])
            source_names=set()
            for path in Path("tools/licensecheck").glob("*_test.go"):
                source_names.update(re.findall(r"(?m)^func (Test[A-Z0-9_][A-Za-z0-9_]*)\(", path.read_text()))
            missing=sorted(source_names-{name.split("/",1)[0] for name in case_names})
            status.update({"source_top_level_count":len(source_names),"missing_cases":missing})
            status.update({"collector_exit":int(v["status"]!="PASS"),"capture_exit":capture_exit,"report_status":v["status"],"case_counts":v["case_counts"],"expected_labels":v["expected_labels"],"missing_count":len(v["missing"]),"missing_labels":sorted({safe_name(str(row.get("label",""))) for row in v["missing"] if isinstance(row,dict)}),"failures":len(v["failures"]),"errors":len(v["errors"]),"nonpassing_cases":nonpass,"execution":execution,"reports":[{"name":r["name"],"sha256":r["sha256"],"bytes":r["bytes"]} for r in records]})
            if v["status"]=="PASS" and set(v["expected_labels"])=={OWNER} and len(execution)==1 and execution[0]=={"strategy":"remote","cached":False,"owner":OWNER} and len(xmls)==1 and len(records)==2 and source_names and not missing:
                status["status"]="PASS"
            elif n or v["status"]=="FAIL": status["status"]="FAIL"
        else:
            counts=[int(c) for c in OK.findall(log)]
            status.update({"counts":counts,"collector_exit":int(not source_ok or len(counts)!=1 or counts[0]<=0)})
            if name=="direct":
                dump(d/"args",args[1:]); (d/"capture").write_text("0\n")
                v=reporter.command_verdict(d/"context");status["report_status"]=v["status"]
                if v["status"]!="PASS":status["collector_exit"]=1
            if n: status["status"]="FAIL"
            elif not status["collector_exit"]: status["status"]="PASS"
    except Exception as error:
        status["diagnostic"]=type(error).__name__
        status["diagnostic_line"]=error.__traceback__.tb_lineno
        status["collector_exit"]=1
        if status["native_exit"] not in (None,0):status["status"]="FAIL"
    return status

if __name__=="__main__":
    pr,rc,out=sys.argv[1:]
    rc=Path(rc).resolve(); out=Path(out).resolve();out.mkdir()
    sha,tree=SOURCES[pr]
    assert git("rev-parse","HEAD","HEAD^{tree}","HEAD^").decode().splitlines()==[sha,tree,PARENT]
    assert not git("status","--porcelain","--untracked-files=no")
    jobs=[("unit",["bazel",f"--bazelrc={rc}","test","--test_env=GO_TEST_WRAP_TESTV=1","--nocache_test_results","--test_sharding_strategy=disabled","--remote_download_regex=.*[/](test[.](log|xml)|test[.]outputs/.*)$",OWNER])]
    result={"pr":pr,"source":sha,"tree":tree,"parent":PARENT,"phases":[]}
    for name,args in jobs:
        result["phases"].append(phase(name,args,out,sha,rc))
        dump(out/"result.json",result)
    result["status"]="PASS" if all(r["status"]=="PASS" for r in result["phases"]) else "FAIL"
    dump(out/"result.json",result)
    print(json.dumps(result,sort_keys=True))
    sys.exit(int(result["status"]!="PASS"))
