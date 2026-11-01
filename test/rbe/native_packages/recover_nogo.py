"""Reconcile the three recorded Nogo TestRunner actions without executing them."""
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import xml.etree.ElementTree as ET
import zipfile

from cas_inputs import blob, cas, input_file, rpc

ORIGINAL = "1b7817e8ada3f2cfa1441307012e8718f613bf064d34af088ea41c041725882c"
CASES = {
    "stock": ("851d04fc2918adcaa60a78fd9fa7e766bc4fec68", "c4e4404c-5266-41ca-a964-0b1e0dfd6d97", "618a3e8a22cbed8a0b51030153601ad637ad0db9f9a240f980898d9d1662969a", 127),
    "clean": ("ace5bfd763bd1fcbeca91d966919cdbd72d53c20", "6605f06f-753f-4a74-ad66-a90cee88686d", "966f9b57eb6ab65de577af793534b9b625f0d8e5ff84714383a971f747e56eb0", 0),
    "diagnostic": ("e99d71d29163b6db3cc456b9e72c1ae4a049f922", "1d73018f-5179-4d8b-914a-95859565dd66", "46342105b1fad89aa826a567410c9d4973ca0fcde03f6291f9253378f911b6dc", 1),
}
OWNER = "//pkg/waiter:waiter_nogo"
BIN = "pkg/waiter/waiter_nogo"
ARCHES = ("amd64", "arm64")
REPORTS = tuple("nogo."+arch+".findings" for arch in ARCHES)
IMAGE = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
MESSAGE = "invalid field access, mu (&({param:q}.mu)) must be locked when accessing list (locks: no locks held)"


def now():
    return datetime.now(timezone.utc).isoformat()


def exactly(items, what):
    values = list(items)
    assert len(values) == 1, f"Missing or ambiguous {what} ({len(values)})"
    return values[0]


def read_inputs(z, variant, result):
    source, inv, action_hash, code = CASES[variant]
    result.update(source=source, invocation=inv, action=action_hash, status="INCOMPLETE", stage="original")
    prefix = variant + "/amd64/"
    prior = json.loads(z.read(prefix+"receipt.json"))
    tests = json.loads(z.read(prefix+"tests.json"))
    result.update(original_producer=prior["status"],original_execution_finished_at=tests["execution_finished_at"],
                  original_verdict_at=prior["verified_at"], original_bazel_finished_at=tests["bep_finish"]["finishTime"])
    assert prior["source"] == tests["source"] == source and tests["expected_labels"] == [OWNER]
    assert prior["phases"]["test"]["invocation_ids"] == [inv]
    assert tests["native_exit"] == (0 if code == 0 else 3)
    native = exactly((w for w in prior["actions"]["tests"] if w["test_binary"] == BIN), "native TestRunner")
    xml_action = exactly((w for w in prior["actions"]["tests"] if w["test_binary"] == "--generate-xml"), "XML action")
    assert len(prior["actions"]["tests"]) == 2 and xml_action["runner"] == "remote" and not xml_action["exit_code"]
    assert native["target"] == OWNER and native["runner"] == "remote" and not native["cache_hit"]
    assert int(native["exit_code"] or 0) == code and native["digest"]["hash"] == action_hash
    assert native["executable"] == "external/bazel_tools/tools/test/posix_tw"
    assert native["platform"] == {"Arch":"amd64","container-image":IMAGE,"workload-isolation-type":"oci"}
    assert not native["test_args"] if variant=="stock" else (native["test_args"][:3]==["filter","-test","-text"] and sorted(native["test_args"][3:])==list(REPORTS))
    result.update(xml_action=xml_action["digest"]["hash"],test_exit=code,platform=native["platform"])
    xml=z.read(prefix+"xml/bazel-out/k8-fastbuild/testlogs/pkg/waiter/waiter_nogo/test.xml")
    suite=ET.fromstring(xml)
    result["original_xml_sha256"]=hashlib.sha256(xml).hexdigest()
    case=exactly(suite.iter("testcase"), "original XML case")
    assert case.get("name") == BIN and (case.find("error") is not None) == (code != 0)
    embedded=exactly(suite.iter("system-out"),"original XML log").text

    result["stage"]="original invocation"
    iv=exactly((v for v in rpc("GetInvocation",{"lookup":{"invocationId":inv}}).get("invocation",[])
                if v.get("invocationId")==inv), "invocation")
    events=[e["buildEvent"] for e in iv.get("event",[]) if "buildEvent" in e]
    finish=exactly((e["finished"] for e in events if "finished" in e), "BEP finish")
    millis = finish.get("finishTimeMillis") or round(datetime.fromisoformat(finish["finishTime"]).timestamp()*1000)
    assert int(millis)==int(tests["bep_finish"]["finishTimeMillis"]) and finish.get("exitCode",{}).get("code",0)==tests["native_exit"]

    result["stage"]="original action"
    matches=rpc("GetExecution",{"executionLookup":{"invocationId":inv,"actionDigestHash":action_hash},"inlineExecuteResponse":True}).get("execution",[])
    execution=exactly((e for e in matches if e.get("actionDigest",{}).get("hash")==action_hash),"remote execution")
    response=execution.get("executeResponse",{})
    assert execution["stage"] in (4,"COMPLETED") and not execution.get("status",{}).get("code",0)
    assert "result" in response and not response.get("status",{}).get("code",0) and not response.get("cachedResult",False)
    action_result=response["result"]
    assert int(action_result.get("exitCode",0))==code
    result["stage"]="original log"
    # The verified XML action consumed test.log and embedded it in system-out.
    # The TestRunner's inline ActionResult does not list that log as an output.
    assert xml_action["test_args"][:2]==["bazel-out/k8-fastbuild/testlogs/"+BIN+"/test.log",
                                         "bazel-out/k8-fastbuild/testlogs/"+BIN+"/test.xml"]
    log=embedded.strip()
    assert "Executing tests from "+OWNER in log
    result["log_source"]="Original generated XML system-out"
    log_diagnostics=[s for s in log.splitlines() if s.startswith("checklocks:")]
    if variant=="stock":
        assert "could not execute " in log and "waiter_nogo: No such file or directory" in log and not log_diagnostics
    elif variant=="clean":
        assert log.splitlines()[-1]=="PASS" and not log_diagnostics
    else:
        assert sorted(log_diagnostics)==sorted(f"checklocks: pkg/waiter/waiter.go:242:4: {MESSAGE} (GOOARCH={arch}, GOOS=linux)" for arch in ARCHES)
    result["log_checklocks"]=log_diagnostics

    result["stage"]="original command"
    action=cas(native["digest"],"Action")
    command=cas(action["commandDigest"],"Command")
    assert command["arguments"]==[native["executable"],BIN,*native["test_args"]]
    platform={p["name"]:p["value"] for p in (action.get("platform") or command.get("platform",{})).get("properties",[])}
    assert all(platform.get(k)==v for k,v in native["platform"].items())
    env={v["name"]:v["value"] for v in command.get("environmentVariables",[])}
    root=action["inputRootDigest"]
    rootpath=PurePosixPath(env.get("TEST_SRCDIR","bazel-out/k8-fastbuild/bin/pkg/waiter/waiter_nogo.runfiles"))
    assert rootpath.name=="waiter_nogo.runfiles" and not rootpath.is_absolute() and ".." not in rootpath.parts
    assert env.get("TEST_WORKSPACE","_main")=="_main"
    runfiles=rootpath/"_main"
    result["stage"]="worker launcher"
    exe=input_file(root,str(runfiles/BIN))
    assert exe.get("isExecutable",False)
    contents=blob(exe["digest"],inv)
    if variant=="stock":
        assert contents.startswith(b"#!/bin/bash\n")
        refs=re.findall(rb"^\S+ filter -test -text (\S+)$",contents,re.M)
        assert sorted(r.decode() for r in refs)==[BIN+"."+a+".findings" for a in ARCHES]
        paths=[r.decode() for r in refs]
    else:
        assert (contents[:7],int.from_bytes(contents[18:20],"little"))==(b"\x7fELF\x02\x01\x01",62)
        paths=native["test_args"][3:]
    result["launcher"]={"kind":"bash" if variant=="stock" else "elf64-x86_64","path":str(runfiles/BIN),"sha256":exe["digest"]["hash"],"size":len(contents)}
    result["reports"]={}
    for name in paths:
        arch=exactly((a for a in ARCHES if name.endswith("."+a+".findings")),"report architecture")
        result["stage"]="worker "+arch+" findings"
        f=input_file(root,str(runfiles/name));raw=blob(f["digest"],inv)
        hits=json.loads(raw)
        if variant=="diagnostic":
            hit=exactly(hits,arch+" finding")
            pos=hit["Position"]
            assert (hit["GOARCH"],hit["GOOS"],hit["Category"],hit["Message"])==(arch,"linux","checklocks",MESSAGE)
            assert pos["Filename"].endswith("pkg/waiter/waiter.go") and (pos["Line"],pos["Column"])==(242,4)
        else:
            assert hits in ([],None) and f["digest"]["hash"]=="38e0b9de817f645c4bec37c0d4a3e58baecccb040f5718dc069a72c7385a0bed"
        result["reports"][arch]={"path":str(runfiles/name),"sha256":f["digest"]["hash"],"size":len(raw),"count":len(hits or [])}
    assert sorted(result["reports"])==list(ARCHES)
    result.update(status="VERIFIED",stage="complete",verified_at=now())


def main():
    out=Path(os.environ["RUNNER_TEMP"])/"native-nogo-original.json"
    result={"status":"INCOMPLETE","new_test_executions":0,"started_at":now(),"original_zip_sha256":ORIGINAL,"prior_incomplete_recovery_run":41298507360,"controls":{}}
    try:
        archive=subprocess.run(["gh","api","repos/tamird/gvisor/actions/artifacts/13079877524/zip"],check=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=90).stdout
        assert hashlib.sha256(archive).hexdigest()==ORIGINAL
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            for variant in CASES:
                row=result["controls"][variant]={}
                try:
                    read_inputs(z,variant,row)
                except Exception as e:
                    # Suppress command arguments, raw command environments and response bodies.
                    row["error"]=str(e)[:190] if type(e) is AssertionError and "api_key" not in str(e) else type(e).__name__
        if all(v.get("status")=="VERIFIED" for v in result["controls"].values()):
            assert result["controls"]["clean"]["launcher"]["sha256"]==result["controls"]["diagnostic"]["launcher"]["sha256"]
            result["status"]="VERIFIED"
    finally:
        result["verdict_at"]=now()
        out.write_text(json.dumps(result,indent=2)+"\n")
    print(result["status"],[(k,v["stage"],v.get("error","")) for k,v in result["controls"].items()],flush=True)
    return 0 if result["status"]=="VERIFIED" else 1


if __name__=="__main__":
    raise SystemExit(main())
