"""Check both address-dump revisions and run the unchanged Netstack bootstrap test."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

import frames
import timer_teardown as public

BASE = "975fb172241c7946a9f67b4f52e0349d9240589a"
FIX = "e18f42092698397c1d1ca248b6bc95bec1c910a2"
DIR = Path("test/packetimpact/netdevs/netlink")
OWNER = "//"+str(DIR)+":netlink_test"
PACKET = "//test/packetimpact/tests:tcp_queue_send_recv_in_syn_sent_netstack_test"
TEST = "TestAddressDumps"
CASES = {"first_interruption_2","first_interruption_10","persistent_interruption","permission",
         "bare_EINTR","completed_cardinality","sibling_address_changes"}
PACKET_CASES = {"TestQueue"+op+"InSynSent"+how for op in ("Send","Recv") for how in ("Handshake","RST")}
WORK = Path(os.environ["RUNNER_TEMP"])/"packet-address-dump"
MODE = sys.argv[1]
assert MODE in ("controls","netstack")
OUT = WORK/MODE
REPORT = {"source":FIX,"input_base":BASE,"original_buildkite":53930,"arms":{},"verified":False,
          "public_inputs":public.seen}


def git(*args):
    return subprocess.check_output(["git",*args])


def save():
    (OUT/"result.json").write_text(json.dumps(REPORT,indent=2,sort_keys=True)+"\n")


def contents(event, owner, stem, row):
    ident,res=event["id"]["testResult"],event["testResult"]
    assert ident["label"].removeprefix("@@")==owner
    row.update(configuration=ident["configuration"]["id"],status=res["status"],files={})
    prefix=Path("bazel-testlogs")/owner[2:].replace(":","/")
    docs={}
    for item in res["testActionOutput"]:
        name=item["name"]
        pcap=name.startswith("test.outputs/") and name.endswith("_tb-0-test.pcap")
        if name not in ("test.xml","test.log") and not pcap:
            continue
        uri=urlsplit(item["uri"])
        assert uri.scheme=="file" and not uri.netloc and ".." not in Path(name).parts
        path=Path(unquote(uri.path))
        assert path.samefile(prefix/name) and 0<path.stat().st_size<=8<<20
        data=path.read_bytes()
        digest=hashlib.sha256(data).hexdigest()
        assert not item.get("digest") or item["digest"]==digest
        assert int(item.get("length",len(data)))==len(data)
        key="tb-0.pcap" if pcap else name
        assert key not in docs
        docs[key]=data
        (OUT/(stem+"-"+key)).write_bytes(data)
        row["files"][name]=[len(data),digest]
    save()
    info=res.get("executionInfo",{})
    assert info.get("strategy") in ("local","standalone") and not res.get("cachedLocally") and not info.get("cachedRemotely")
    assert all(ident.get(k,1)==1 for k in ("attempt","run","shard"))
    return docs


def assess(arm,row,docs):
    log=docs["test.log"].decode(errors="replace").replace("\x16","")
    xml=ElementTree.fromstring(docs["test.xml"])
    names={n.get("name"):n for n in xml.iter("testcase")}
    assert not any(e.tag=="skipped" for e in xml.iter())
    status="FAIL" if arm=="baseline" else "PASS"
    terminal=re.findall(r"(?m)^\s*--- (PASS|SKIP|FAIL): (\S+) [(]",log)
    if arm=="netstack":
        assert set(names)==PACKET_CASES and sorted(terminal)==sorted(("PASS",n) for n in PACKET_CASES)
        assert not any(e.tag in ("failure","error") for e in xml.iter())
        tcp=[p for p in frames.frames(docs["tb-0.pcap"]) if p["family"]==4 and p["proto"]==6]
        row["capture"]={"syn":sum((p["flags"]&0x12)==2 for p in tcp),"rst":sum(bool(p["flags"]&4) for p in tcp),
                        "sample_data":sum(p["data"]==b"Sample Data" for p in tcp)}
        assert all(row["capture"].values()),row["capture"]
    else:
        expected={"first_interruption_10"} if arm=="baseline" else CASES
        leaves={TEST+"/"+n for n in expected}
        assert leaves <= names.keys() <= leaves|{TEST},sorted(names)
        for name in leaves:
            assert (status,name) in terminal and any(e.tag in ("failure","error") for e in names[name])==(arm=="baseline"),name
        if arm=="baseline":
            assert "attempts=1" in log and "failed to get ipv6 addrs: injected after kernel dump: results may be incomplete or inconsistent" in log
        else:
            assert not any(e.tag in ("failure","error") for e in xml.iter())
            row["observed_kernel_interruptions_in_churn"]=int(re.search(r"lookups=16 sibling_add_delete_pairs=16 actual_kernel_interruptions=(\d+)",log)[1])
    assert row["exit"]==(3 if arm=="baseline" else 0) and row["status"]==("FAILED" if arm=="baseline" else "PASSED")
    row["leaves"]=sorted(names)
    row["verified"]=True


def run(arm,row,deadline):
    owner=PACKET if arm=="netstack" else OWNER
    bep=WORK/(arm+".bep")
    args=[str(WORK/"bazel"),"--nosystem_rc","--nohome_rc","--bazelrc=/dev/null","--host_jvm_args=-Xmx4g","test","--config=x86_64",
          "--jobs=4","--lockfile_mode=off","--remote_executor=","--remote_cache=","--bes_backend=",
          "--spawn_strategy=processwrapper-sandbox,local","--strategy=GoStdlib=processwrapper-sandbox",
          "--strategy=GoStdlibList=processwrapper-sandbox","--strategy=TestRunner=local","--nocache_test_results",
          "--flaky_test_attempts=1","--test_sharding_strategy=disabled","--test_env=GO_TEST_WRAP_TESTV=1",
          "--build_event_json_file="+str(bep),"--nobuild_event_json_file_path_conversion"]
    if owner==OWNER:
        args+=["--test_timeout=90","--run_under=/usr/bin/unshare --user --map-root-user --net --",
               "--test_env=DUMP_PARENT_NET="+os.readlink("/proc/self/ns/net"),
               "--test_env=DUMP_PARENT_USER="+os.readlink("/proc/self/ns/user"),
               "--test_arg=-test.v","--test_filter=^"+TEST+"$"+("/^first_interruption_10$" if arm=="baseline" else "")]
    else:
        args+=["--test_timeout=360","--nozip_undeclared_test_outputs"]
        assert not git("diff","--name-only","HEAD") and git("rev-parse","HEAD").decode().strip()==FIX
    row["owner"]=owner
    for _ in range(8):
        bep.unlink(missing_ok=True)
        remaining=int(deadline-time.monotonic())
        if remaining<=0:
            raise TimeoutError("arm budget expired")
        proc=subprocess.run(["timeout","--signal=INT","--kill-after=5s",str(remaining)+"s",*args,
                             *("--distdir="+str(p) for p in sorted(public.dirs)),owner],
                            stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        row["exit"]=proc.returncode
        text=proc.stdout.decode(errors="replace")
        row["diagnostics"]=[l[:400] for l in text.splitlines() if l.startswith(("ERROR:","FATAL:")) or re.search(r"[.]go:\d+:",l)][:10]
        events=[json.loads(l) for l in bep.read_bytes().splitlines(keepends=True) if l.endswith(b"\n")] if bep.exists() else []
        results=[e for e in events if "testResult" in e]
        save()
        if results or proc.returncode==124 or not public.acquire(text):
            break
    event,=results
    docs=contents(event,owner,arm,row)
    assert any((t:=e["id"].get("targetCompleted",{})).get("label","").removeprefix("@@")==owner and t.get("configuration",{}).get("id")==row["configuration"] for e in events)
    assess(arm,row,docs)


def main():
    donor=os.environ["GITHUB_SHA"]
    OUT.mkdir(exist_ok=True,parents=True)
    assert git("rev-parse","HEAD").decode().strip()==donor and git("rev-parse",donor+"^").decode().strip()==FIX
    assert git("rev-parse",FIX+"^").decode().strip()==BASE and git("diff","--name-only",BASE,FIX).decode().splitlines()==[str(DIR/"netlink.go")]
    public.assets.DIST.mkdir(parents=True,exist_ok=True)
    public.inputs.LIMIT=min(public.inputs.LIMIT,384<<20)
    public.known.update((v[2],v) for v in json.loads((WORK/"inputs.json").read_bytes()))
    testfile=DIR/"netlink_test.go"
    build=git("show",donor+":"+str(DIR/"BUILD"))
    fixture=git("show",donor+":"+str(testfile))
    for arm,source,budget in ((("baseline",BASE,9),("candidate",FIX,8)) if MODE=="controls" else (("netstack",FIX,22),)):
        row=REPORT["arms"][arm]={"verified":False,"source":source}
        save()
        git("switch","--quiet","--detach",source)
        path=DIR/"netlink.go"
        assert not testfile.exists() and not git("diff","--name-only","HEAD")
        try:
            if arm!="netstack":
                code=path.read_text()
                assert code.count("netlink.AddrList(")==(2 if arm=="baseline" else 1)
                code=code.replace("netlink.AddrList(","queryAddrs(")
                before="func addrList(" if arm=="candidate" else "// IfaceInfo returns"
                assert code.count(before)==1
                path.write_text(code.replace(before,"// queryAddrs is replaced only in the private qualification fixture.\nvar queryAddrs = netlink.AddrList\n\n"+before))
                if arm=="candidate":
                    assert path.read_bytes()==git("show",donor+":"+str(path))
                (DIR/"BUILD").write_bytes(build)
                testfile.write_bytes(fixture)
            run(arm,row,time.monotonic()+budget*60)
        except Exception as exc:
            row["error"]=type(exc).__name__+" "+str(exc)[:300]
        finally:
            git("restore","--",str(path),str(DIR/"BUILD"))
            testfile.unlink(missing_ok=True)
            git("switch","--quiet","--detach",donor)
            save()
    REPORT["verified"]=all(r["verified"] for r in REPORT["arms"].values())
    save()
    assert REPORT["verified"]


if __name__=="__main__":
    main()
