"""Read the original local Go test output for the permanent regression cases."""
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

from qualify import W,OUT,SOURCE,sha

CASES={
    "//pkg/tcpip/link/fdbased:fdbased_test":["TestWritePacket","TestHashedWritePacket"],
    "//pkg/tcpip/transport/tcp/test/e2e:tcp_test":[
        name+tail for name in ("TestSetExperimentOption","TestSetExperimentOptionIPv6")
        for tail in ("","/host_GSO=false","/host_GSO=true")],
}
source=subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()
assert source==SOURCE
bep=W/"controls.bep"
args=[W/"bazel","--nosystem_rc","--nohome_rc","--host_jvm_args=-Xmx4g","test","--config=x86_64","-c","opt","--jobs=4","--lockfile_mode=off","--remote_executor=","--remote_cache=","--bes_backend=","--strategy=TestRunner=local","--nocache_test_results","--test_sharding_strategy=disabled","--flaky_test_attempts=1","--test_tag_filters=","--build_tag_filters=","--test_env=GO_TEST_WRAP_TESTV=1","--test_arg=-test.v","--test_arg=-test.run=^Test(SetExperimentOption(IPv6)?|WritePacket|HashedWritePacket)$","--build_event_json_file="+str(bep),*CASES]
with (W/"controls.bazel.log").open("w") as f:
    code=subprocess.run(args,stdout=f,stderr=subprocess.STDOUT).returncode
report={"source":source,"carrier":os.environ["GITHUB_SHA"],"exit":code,"cases":{}}
if code:
    report["diagnostic"]=[line[:400] for line in (W/"controls.bazel.log").read_text(errors="replace").splitlines() if "ERROR:" in line or ".go:" in line or "FAIL:" in line][:16]
def save():
    (OUT/"controls.json").write_text(json.dumps(report,indent=2,sort_keys=True)+"\n")
save()
events=[json.loads(s) for s in bep.read_text().splitlines()]
for e in events:
    if "testResult" not in e:continue
    ev=e["id"]["testResult"]
    owner=ev["label"].removeprefix("@@")
    assert owner in CASES and owner not in report["cases"]
    res=e["testResult"]
    row=report["cases"][owner]={"status":res["status"],"strategy":res.get("executionInfo",{}).get("strategy"),"configuration":ev["configuration"]["id"]}
    for name in ("test.log","test.xml"):
        a,=[a for a in res["testActionOutput"] if a["name"]==name]
        u=urlsplit(a["uri"])
        p=Path(unquote(u.path))
        assert u.scheme=="file" and not(u.netloc or u.query or u.fragment)
        assert p.samefile(Path("bazel-testlogs")/owner[2:].replace(":","/")/name) and p.stat().st_size<2<<20
        data=p.read_bytes()
        assert not a.get("digest") or a["digest"]==sha(data)
        assert len(data)==int(a.get("length",len(data)))
        (OUT/(owner.split(":")[-1]+"-"+name)).write_bytes(data)
        row[name]=sha(data)
        if name=="test.log":log=data.decode(errors="replace").replace("\x16","")
        else:xml=ET.fromstring(data)
    save()
    assert not res.get("cachedLocally") and not res.get("executionInfo",{}).get("cachedRemotely")
    assert row["status"]=="PASSED" and row["strategy"] in ("local","standalone")
    assert all(int(ev.get(k,1))==1 for k in ("shard","run","attempt"))
    assert xml.findall(".//testcase") and all(xml.find(".//"+k) is None for k in ("failure","error","skipped"))
    for name in CASES[owner]:
        assert len(re.findall(r"(?m)^\s*--- PASS: "+re.escape(name)+r" \(",log))==1,name
    row["named_passes"]=CASES[owner]
    save()
assert set(report["cases"])==set(CASES)
assert code==0 and [e["finished"]["exitCode"].get("code",0) for e in events if "finished" in e]==[0]
report["verified"]=True
save()
