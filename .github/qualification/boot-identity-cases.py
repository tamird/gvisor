"""Record the exact named syscall test outcome using Bazel's own XML and log."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

stage = sys.argv[1]
tmp = Path(os.environ["RUNNER_TEMP"])
proof = tmp/"boot-identity-results"
source = subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()
product = os.environ["STOCK_PRODUCT" if stage == "stock" else "FIX_PRODUCT"]
named = "MountTest.UserNamespaceProcMount"
owners = ["//pkg/sentry/fsimpl/proc:"+s for s in ("proc_test","proc_nogo","proc_test_nogo")]
platform = "native" if stage == "native" else "runsc_systrap_directfs"
target = "//test/syscalls:mount_test_"+platform
labels = owners if stage == "owners" else [target]
now = lambda: datetime.datetime.now(datetime.UTC).isoformat()
out = dict(source=source,product=product,expected=labels,started=now(),status="FAIL",failures=[],skips=[])
guest = ["docker","exec","-e","GIT_CONFIG_COUNT=1","-e","GIT_CONFIG_KEY_0=safe.directory",
         "-e","GIT_CONFIG_VALUE_0="+os.environ["GITHUB_WORKSPACE"],"overlay-guest"]
try:
    assert source == os.environ["BASE" if stage == "stock" else "SOURCE"], "wrong source"
    out["fixture_sha256"] = hashlib.sha256(Path("test/syscalls/linux/mount.cc").read_bytes()).hexdigest()
    bep = tmp/(stage+".bep")
    logs = tmp/(stage+".bazel.log")
    flags = ["--config=x86_64","--platforms=//tools/bazeldefs:linux_amd64","-c","opt",
             "--jobs=4" if stage=="owners" else "--jobs=32","--keep_going","--stamp","--workspace_status_command=",
             "--build_event_json_file="+str(bep),"--remote_download_outputs=toplevel",
             "--remote_download_regex=.*[/]test[.](log|xml)$","--nocache_test_results","--test_output=all",
             "--test_timeout=600","--test_sharding_strategy=disabled"]
    if stage == "owners":
        flags += ["--strategy=GoStaticAnalysis=linux-sandbox","--strategy=GoStandardLibraryAnalysis=linux-sandbox",
                  "--strategy=GoStdlib=linux-sandbox","--strategy=GoStdlibList=linux-sandbox",
                  "--modify_execution_info=^Go(StandardLibrary|Static)Analysis$=-no-sandbox,^Go(Stdlib(List)?|StandardLibraryAnalysis)$=+no-remote-cache"]
    else:
        flags += ["--strategy=TestRunner="+("local" if stage=="native" else "remote"),
                  "--modify_execution_info=^TestRunner$=+no-remote-cache","--test_env=GTEST_FILTER="+named]
    with logs.open("wb") as dest:
        out["exit"]=subprocess.run([*guest,"bazel","--bazelrc="+str(tmp/"overlay.bazelrc"),"test",*flags,*labels],
                                   stdout=dest,stderr=subprocess.STDOUT).returncode
    out["execution_finished_at"]=now()
    evs=[json.loads(row) for row in bep.read_text().splitlines()] if bep.exists() else []
    statuses={e["id"]["testSummary"]["label"]:e["testSummary"]["overallStatus"] for e in evs if "testSummary" in e.get("id",{})}
    text=logs.read_text(errors="replace")
    out.update(summaries=statuses, missing=sorted(set(labels)-set(statuses)),
               invocations=sorted(set(re.findall(r"https://app.buildbuddy.io/invocation/[0-9a-f-]+",text))))
    if stage=="owners":
        assert out["exit"]==0 and statuses==dict.fromkeys(labels,"PASSED"), "missing/failed package or analysis"
    else:
        path = "bazel-testlogs/test/syscalls/mount_test_"+platform+"/"
        log = subprocess.check_output([*guest,"cat",path+"test.log"],text=True,errors="replace")
        xml = subprocess.check_output([*guest,"cat",path+"test.xml"])
        rows={}
        for r in ET.fromstring(xml).iter("testcase"):
            name=r.get("classname","")+"."+r.get("name","")
            assert name not in rows, "duplicate "+name
            rows[name]=("skip" if r.find("skipped") is not None or r.get("result") in ("skipped","suppressed") or r.get("status")!="run" else
                        "fail" if any(t.tag in ("failure","error") for t in r) else "pass")
        out.update(named_cases=rows,skips=[k for k,v in rows.items() if v=="skip"])
        out["case_output"]=[line.strip()[:340] for line in log.splitlines() if named in line or
             "mounted_id == boot_id" in line or "check failed" in line.lower() or "mount.cc:" in line][-24:]
        stock=stage=="stock"
        assert rows=={named:"fail" if stock else "pass"}, "unexpected, missing or skipped named case"
        assert statuses=={target:"FAILED" if stock else "PASSED"} and out["exit"]==(3 if stock else 0), "unexpected Bazel outcome"
        if stock:
            assert "mounted_id == boot_id" in log, "expected comparison not observed"
    out["status"]="PASS"
except Exception as error:
    out["failures"].append(str(error)[:1000])
    if (tmp/(stage+".bazel.log")).exists():
        text=(tmp/(stage+".bazel.log")).read_text(errors="replace")
        lines=[l for l in text.splitlines() if re.search(r"(ERROR:|error:|FAILED|fatal:|Skipping|No test)",l)]
        out["diagnostics"]=[re.sub(r"(?i)(x-buildbuddy-api-key[=: ]+)\S+",r"\1[redacted]",l[:800]) for l in lines[-24:]]
finally:
    out.setdefault("execution_finished_at",now())
    out.setdefault("missing",labels)
    out["verdict_available_at"]=now()
    (proof/(stage+".json")).write_text(json.dumps(out,indent=2)+"\n")
if out["status"] != "PASS":
    raise SystemExit(1)
