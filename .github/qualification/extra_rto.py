"""Retain every matching wire record, even for the expected failing controls."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

tmp=Path(os.environ["RUNNER_TEMP"])
out=tmp/"extra-rto"
fixture="c92ffa33396979601786e21f372e2515506a7c8c"
master="f51a205a8f4afbc5ab41aded070df74951b081ea"
candidate="cc572743e815dc6c99dabc4f9fb5a052d8be4851"
files={"test/packetimpact/tests/tcp_user_timeout_test.go":"b6f2c5337dbe905dabd3e7bf334307bf7b958665",
       "test/packetimpact/testbench/connections.go":"9e236c7566ada8b2d3a497beb9797a765e816bda"}
report={"fixture":fixture,"files":files,"runs":[]}

def git(*args):
    return subprocess.check_output(["git",*args],text=True).strip()

def save():
    (out/"result.json").write_text(json.dumps(report,indent=2)+"\n")

def facts(row):
    o=row.get("result",{})
    if not o:return False
    events=row["wire"]
    domain={"w":0,"a":0,"b":512,"c":1024,"d":1536}
    wire_times=[stamp for ch,offs,stamp in events if ch!="w"]
    spacing=o["retry_us"]-o["fresh_us"]
    return (all(off==domain[ch] for ch,off,_ in events) and wire_times==sorted(wire_times)
        and sum(ch=="a" for ch,_,_ in events)>=3
        and any(ch=="b" and t>=o["fresh_us"] and abs(t-o["retry_us"])<=2500 for ch,off,t in events)
        and 0<o["first_us"]<o["second_us"]<=o["fresh_us"] and spacing>=o["rto_us"]//2
        and o["retry_us"]<=o["ack_us"]<o["uto_us"]<o["probe_us"] and o["probe_us"]-o["fresh_us"]<o["uto_us"]
        and o["errno"] in (0,110) and bool([e for e in events if e[0]=="d"])==(o["errno"]==0)
        and row["exit"]==(0 if o["errno"]==0 else 3) and row.get("test")==("PASS" if o["errno"]==0 else "FAIL"))

for arm,source,variant in (("linux",master,"native"),("stock",master,"netstack"),("candidate",candidate,"netstack")):
    git("checkout","--quiet","--detach",source)
    git("restore","--source="+fixture,"--worktree","--",*files)
    assert all(git("hash-object",p)==v for p,v in files.items())
    owner="//test/packetimpact/tests:tcp_user_timeout_"+variant+"_test"
    for trial in (1,2):
        row={"arm":arm,"source":source,"snd_blob":git("hash-object","pkg/tcpip/transport/tcp/snd.go"),"trial":trial,"owner":owner,"wire":[],"info":{},"notes":[]}
        assert row["snd_blob"]==git("rev-parse",source+":pkg/tcpip/transport/tcp/snd.go")
        report["runs"].append(row);save()
        log=out/(arm+str(trial)+".log")
        with log.open("w") as f:
            row["exit"]=subprocess.run([str(tmp/"bazel"),"--nosystem_rc","--nohome_rc","--host_jvm_args=-Xmx4g","test",
                "--config=x86_64","--jobs=3","--strategy=TestRunner=local","--test_env=GO_TEST_WRAP_TESTV=1",
                "--test_filter=^TestUserTimeoutThroughInterveningRTO$","--test_sharding_strategy=disabled",
                "--nocache_test_results","--flaky_test_attempts=1","--test_timeout=200","--test_output=all",owner],stdout=f,stderr=subprocess.STDOUT).returncode
        for line in log.read_text(errors="replace").splitlines():
            if m:=re.search(r"MULTI_WIRE letter=([a-dw]) offset=(\d+) us=(\d+)",line):
                row["wire"].append([m[1],int(m[2]),int(m[3])])
            if m:=re.search(r"MULTI_INFO phase=([bc]) rto_us=(\d+) retransmits=(\d+) backoff=(\d+)",line):
                row["info"][m[1]]=list(map(int,m.groups()[1:]))
            if m:=re.search(r"MULTI_RESULT ((?:[a-z0-9_]+=\d+ ?)+)",line):
                row["result"]={k:int(v) for k,v in (kv.split("=") for kv in m[1].split())}
            if m:=re.search(r"--- (PASS|FAIL|SKIP): TestUserTimeoutThroughInterveningRTO ",line): row["test"]=m[1]
            if re.search(r"tcp_user_timeout_test.go:\d+:|go:.*undefined|^ERROR:|build failed",line) and not re.search(r"(?i)token|authorization|cookie|secret",line):
                if len(row["notes"])<15:row["notes"].append(line.strip()[:275])
        row["log_sha256"]=hashlib.sha256(log.read_bytes()).hexdigest()
        row["verified"]=facts(row);save()
        print(arm,trial,row["verified"],row.get("result"),flush=True)
    git("restore","--source=HEAD","--worktree","--",*files)
report["verified"]=len(report["runs"])==6 and all(r["verified"] for r in report["runs"])
save()
raise SystemExit(not report["verified"])
