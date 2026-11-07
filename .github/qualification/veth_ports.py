"""Qualify the unchanged netlink/syscall test on the stock and fixed veth."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

STOCK = "ba42aa160e3fec678426c4b6037a5b715b545506"
FIXED = "fea7e15b6953bd487be94e0f7bff8b20cc4ea7a8"
ROUTE = "//test/syscalls:socket_netlink_route_test_"
TEST = "NetlinkRouteTest.VethAdd"
PHASES = [
    ("linux", STOCK, ROUTE+"native", TEST),
    ("stock", STOCK, ROUTE+"runsc_systrap_directfs", TEST),
    ("fixed", FIXED, ROUTE+"runsc_systrap_directfs", TEST),
    ("checkpoint", FIXED, "//pkg/tcpip/link/veth:veth_test", "TestMTU"),
    ("tuntap", FIXED, "//test/syscalls/linux:tuntap_test", None),
]


def root_of(label):
    return Path("bazel-testlogs")/label[2:].replace(":", "/")


def run(work):
    for phase, revision, label, case in PHASES:
        subprocess.run(["git","checkout","-q","--detach",revision],check=True)
        assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==revision
        d=work/phase
        d.mkdir()
        cmd=[str(work/"bazel"),"--nosystem_rc","--nohome_rc","--bazelrc="+str(work/"private.rc"),
             "test" if case else "build","--config=x86_64","--jobs=24","--remote_download_outputs=minimal",
             "--build_event_json_file="+str(d/"events"),"--color=no","--curses=no","--noshow_progress"]
        if case:
            cmd+=["--nocache_test_results","--flaky_test_attempts=1","--test_sharding_strategy=disabled",
                  "--modify_execution_info=^TestRunner$=+no-remote-cache","--test_env=GO_TEST_WRAP_TESTV=1",
                  "--remote_download_regex=.*[/]testlogs/.*/test[.](xml|log)$"]
            if case==TEST:
                cmd.append("--test_filter="+case)
            else:
                cmd+=["--test_arg=-test.run=^TestMTU$","--test_arg=-test.v"]
        with (d/"console").open("wb") as log:
            rc=subprocess.run(cmd+[label],stdout=log,stderr=subprocess.STDOUT).returncode
        (d/"exit").write_text(str(rc))
        if case:
            for n in ("test.xml","test.log"):
                p=root_of(label)/n
                if p.is_file():
                    shutil.copyfile(p,d/n)


def verified_file(entry,d):
    name=entry["name"]
    data=(d/name).read_bytes()
    digest=hashlib.sha256(data).hexdigest()
    match=re.search(r"/(?:blobs|compressed-blobs/zstd)/([0-9a-f]{64})/(\d+)(?:$|[?])",entry.get("uri",""))
    if match:
        assert (digest,len(data))==(match[1],int(match[2]))
    else:
        assert entry.get("digest")==digest and int(entry.get("length",-1))==len(data)
    return data,digest


def collect(work):
    receipt={"fixed":FIXED,"stock":STOCK,"phases":{}}
    for phase,rev,label,case in PHASES:
        d=work/phase
        row=receipt["phases"][phase]={"source":rev,"label":label,"verified":False}
        try:
            rc=int((d/"exit").read_text());row["exit"]=rc
            events=[json.loads(l) for l in (d/"events").read_text().splitlines()]
            row["invocation"]=str(uuid.UUID(next(e["started"]["uuid"] for e in events if "started" in e)))
            terminal=next(e["finished"]["exitCode"].get("code",0) for e in events if "finished" in e)
            assert terminal==rc
            signals=("permission denied","operation not permitted","not declared in package","unknown target","no such file","undefined reference","no registered executors","error loading package","no matching execution platform","no space left","killed","undeclared identifier","not a member","not permitted in a constexpr")
            text=(d/"console").read_text(errors="replace").lower()
            row["signals"]=[s for s in signals if s in text]
            if not case:
                row["verified"]=rc==0 and any(e.get("id",{}).get("targetCompleted",{}).get("label")==label and not e["id"]["targetCompleted"].get("aspect") and e.get("completed",{}).get("success") for e in events)
                continue
            results=[e["testResult"] for e in events if "testResult" in e and e["id"]["testResult"]["label"]==label];row["attempts"]=len(results)
            assert len(results)==1
            r=results[0];info=r.get("executionInfo",{})
            row.update(status=r["status"],strategy=info.get("strategy"),cached=bool(r.get("cachedLocally") or info.get("cachedRemotely")))
            files={f["name"]:f for f in r["testActionOutput"]}
            xml,row["xml"]=verified_file(files["test.xml"],d);raw,row["log"]=verified_file(files["test.log"],d)
            log=raw.decode(errors="replace");nodes=list(ET.fromstring(xml).iter("testcase"))
            assert len(nodes)==1
            tc=nodes[0];full=tc.get("classname","")+"."+tc.get("name","")
            assert full.endswith(case) and tc.find("skipped") is None and tc.get("result") not in ("skipped","suppressed") and tc.get("status")!="notrun"
            failures=list(tc.findall("failure"))+list(tc.findall("error"))
            row["case"]=case;row["failures"]=len(failures)
            assert row["strategy"]=="remote" and not row["cached"]
            if phase=="stock":
                row["source_failures"]=[]
                for f in failures:
                    m=f.get("message","")+" "+(f.text or "")
                    row["source_failures"].append({"lines":sorted({int(n) for n in re.findall(r"socket_netlink_route[.]cc:(\d+)",m)})[:4],
                        "peer_expr":"peer.mtu" in m,"readbacks":[int(v) for v in re.findall(r"Which is: (\d+)",m)][:4],
                        "ports":sorted(set(re.findall(r"\bveth[12]\b",m)))})
                pairs=[]
                for f in failures:
                    message=f.get("message","")+" "+(f.text or "")
                    if "peer.mtu" not in message:continue
                    numbers=[int(v) for v in re.findall(r"Which is: (\d+)",message)][:2]
                    trace=re.search(r"socket_netlink_route[.]cc:\d+: (veth[12])\b",message)
                    if len(numbers)==2 and numbers[0]!=numbers[1] and trace:pairs.append([trace[1],*numbers])
                row["peer_mismatches"]=pairs
                row["verified"]=rc==3 and row["status"]=="FAILED" and len(failures)==2 and sorted(p[0] for p in pairs)==["veth1","veth2"]
            else:
                pattern=r"\[\s*OK\s*\]\s+NetlinkRouteTest\.VethAdd\b" if case==TEST else r"--- PASS: TestMTU\b"
                row["log_pass"]=bool(re.search(pattern,log))
                row["verified"]=rc==0 and row["status"]=="PASSED" and not failures and row["log_pass"]
        except (AssertionError,IndexError,KeyError,OSError,ValueError,StopIteration,ET.ParseError) as e:
            row["error"]=type(e).__name__
    receipt["verified"]=all(r["verified"] for r in receipt["phases"].values())
    (work/"result.json").write_text(json.dumps(receipt,indent=2)+"\n")
    return not receipt["verified"]


if __name__=="__main__":
    directory=Path(sys.argv[2])
    if sys.argv[1]=="run":run(directory)
    else:raise SystemExit(collect(directory))
