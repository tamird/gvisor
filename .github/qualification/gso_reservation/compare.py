"""One local Linux comparison of stock and revised TCP software segmentation."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

from fixture import install

BASE = "7d1b049f5d541992b47cc38cf564025908a9ed7b"
FIX = "e45857ee5044d16ec88eab54f9f9dfa793940712"
TCP = Path("pkg/tcpip/transport/tcp")
SRC = TCP / "connect.go"
INTERNAL = "//pkg/tcpip/transport/tcp:tcp_test"
E2E = "//pkg/tcpip/transport/tcp/test/e2e:tcp_test"
WORK = Path(os.environ["RUNNER_TEMP"]) / "gso"
OUT = WORK / "out"
BZ = [WORK / "bazel", "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g"]
FLAGS = ["--config=x86_64", "-c", "opt", "--jobs=4", "--remote_executor=", "--remote_cache=", "--bes_backend=", "--lockfile_mode=off", "--test_tag_filters=", "--build_tag_filters="]
CPU = min(os.sched_getaffinity(0))
REPORT = {"base": BASE, "product": FIX, "carrier": os.environ["GITHUB_SHA"], "core": CPU, "runs": {}, "verified": False}


def sha(b):
    return hashlib.sha256(b).hexdigest()


def save():
    (OUT / "result.json").write_text(json.dumps(REPORT, indent=2, sort_keys=True) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def bazel(stage, cmd, *args):
    with (WORK / (stage + ".out")).open("w") as log:
        rc = subprocess.run([*BZ, cmd, *FLAGS, *args], stdout=log, stderr=subprocess.STDOUT).returncode
    if rc:
        plain = (WORK / (stage + ".out")).read_text(errors="replace").splitlines()
        REPORT["runs"][stage]["diagnostics"] = [s[:420] for s in plain if "ERROR:" in s or ".go:" in s or "FAIL:" in s][:15]
        save()
    return rc


def outputs(result, owner, stage):
    files = {}
    for name in ("test.log", "test.xml"):
        f, = [f for f in result["testActionOutput"] if f["name"] == name]
        uri = urlparse(f["uri"])
        path = Path(unquote(uri.path))
        assert uri.scheme == "file" and not (uri.netloc or uri.query or uri.fragment)
        assert path.samefile(Path("bazel-testlogs")/owner[2:].replace(":", "/")/name)
        assert path.stat().st_size < 1 << 20
        data = path.read_bytes()
        assert int(f.get("length", len(data))) == len(data)
        assert not f.get("digest") or f["digest"] == sha(data)
        (OUT / (stage + "." + name)).write_bytes(data)
        files[name] = data
    return files


def measure(stage, source, owner, case=None):
    row = REPORT["runs"][stage] = {"source": source, "owner": owner}
    git("restore", "--source", source, "--", str(SRC))
    code = git("hash-object", str(SRC))
    assert code == git("rev-parse", source + ":" + str(SRC))
    row["source_blob"] = code
    bep = WORK / (stage+".bep")
    args = ["--test_arg=-test.run=^" + (case or "") + "$", "--test_env=GO_TEST_WRAP_TESTV=1", "--test_arg=-test.v"]
    if not case:
        args += ["--test_arg=-test.bench=^BenchmarkIPv6SoftwareGSO$", "--test_arg=-test.benchtime=1000x", "--test_arg=-test.cpu=1", "--test_arg=-test.benchmem"]
    row["exit"] = bazel(stage, "test", "--strategy=TestRunner=local", "--nocache_test_results", "--test_sharding_strategy=disabled",
                        "--flaky_test_attempts=1", "--test_timeout=150", "--run_under=/usr/bin/taskset -c "+str(CPU),
                        "--build_event_json_file="+str(bep), *args, owner)
    events = [json.loads(s) for s in bep.read_text().splitlines()]
    ev, = [e for e in events if "testResult" in e]
    result, ident = ev["testResult"], ev["id"]["testResult"]
    assert ident["label"].removeprefix("@@") == owner
    row["invocation"], = [e["started"]["uuid"] for e in events if "started" in e]
    assert all(int(ident.get(k, 1)) == 1 for k in ("run","attempt","shard"))
    files = outputs(result, owner, stage)
    row["digests"] = {n:sha(data) for n,data in files.items()}
    row["strategy"] = result.get("executionInfo", {}).get("strategy")
    row["cached"] = bool(result.get("cachedLocally") or result.get("executionInfo", {}).get("cachedRemotely"))
    row["status"] = result["status"]
    save()
    assert row["exit"] == 0 and row["status"] == "PASSED" and not row["cached"] and row["strategy"] in ("local","standalone")
    assert [e["finished"]["exitCode"].get("code",0) for e in events if "finished" in e] == [0]
    xt = ET.fromstring(files["test.xml"])
    assert xt.find(".//failure") is None and xt.find(".//error") is None
    cases = list(xt.iter("testcase"))
    if case:
        assert cases and all(c.find("skipped") is None and c.get("result") != "suppressed" and c.get("status") not in ("notrun","skipped") for c in cases)
    log = files["test.log"].decode().replace("\x16","")
    if owner == INTERNAL:
        execution, = set(re.findall(r"GSO_OWNER sha=([a-f0-9]{64}) cpu=([^\s]+)", log))
        row["binary"], pinned = execution
        assert pinned == str(CPU)
    if case:
        for name in (case,case+"/option_0",case+"/option_99"):
            assert len(re.findall(r"(?m)^\s*=== RUN\s+"+re.escape(name)+r"\s*$",log))==1
            assert len(re.findall(r"(?m)^\s*--- PASS: "+re.escape(name)+r" \(",log))==1
        tag = "GSO_PACKET" if owner == INTERNAL else "SOCKET_PACKET"
        packets = [dict(re.findall(r"(\w+)=(\w+)", line.split(tag+" ",1)[1])) for line in log.splitlines() if tag+" " in line]
        row["packets"] = packets
        save()
        assert len(packets)==16
        for opt in (0,99):
            arm=[p for p in packets if int(p["opt"])==opt]
            assert len(arm)==8
            tail = int(arm[-1]["reserve"])
            assert tail == (68 if opt else 60)
            for i,p in enumerate(arm):
                assert int(p["offset"])==1440*i and int(p["data"])==(73 if i==7 else 1440)
                extra = i*8 if opt and source==BASE and i<7 else 0
                assert int(p["reserve"])==tail+extra and int(p["available"])==extra
                assert int(p["wire"])==int(p["data"])+tail
                if tag=="GSO_PACKET":
                    assert int(p["index"])==i and (p["reused"]=="true")==(i==7)
    else:
        rows = re.findall(r"(?m)^BenchmarkIPv6SoftwareGSO/option_(?:0|99)(?:-\d+)?[ \t]+1000[ \t]+[^\r\n]*",log)
        assert len(rows)==2 and "option_0" in rows[0] and "option_99" in rows[1]
        assert all("ns/op" in s and "B/op" in s and "allocs/op" in s and "reserved-B/op" in s for s in rows)
        for opt, line in zip((0,99), rows):
            reserved, = re.findall(r"\b([0-9.]+) reserved-B/op\b", line)
            segments = (32000+1439)//1440
            extra = 8*sum(range(segments-1)) if opt and source==BASE else 0
            assert float(reserved) == (68 if opt else 60)*segments+extra
        row["benchmark"] = rows
    row["verified"] = True
    save()


def run_cell(*args):
    try:
        measure(*args)
    except Exception as err:
        REPORT["runs"][args[0]]["error"] = f"{type(err).__name__}: {err}"[:500]
        save()
        return False
    return True


def main():
    assert platform.system()=="Linux" and platform.machine()=="x86_64"
    assert sha((WORK/"benchstat.module").read_bytes())=="373bba0a70f04c2a39a30a45b4acd23f3bfa884e05a5e27cce1ce7010c406cd5"
    assert git("rev-parse","HEAD")==FIX and git("rev-parse","HEAD^")==BASE
    install(WORK)
    REPORT["fixtures"]={str(p):sha(p.read_bytes()) for p in (TCP/"gso_reservation_test.go",TCP/"BUILD",TCP/"test/e2e/tcp_test.go")}
    save()
    for name,source in (("stock",BASE),("fixed",FIX)):
        run_cell(name+"-sender",source,INTERNAL,"TestGSOExperimentReservation")
        run_cell(name+"-socket",source,E2E,"TestSetExperimentOptionIPv6")
    assert all(REPORT["runs"][s+"-sender"].get("verified") for s in ("stock","fixed"))
    for i in range(12):
        for name,source in ((("stock",BASE),("fixed",FIX)) if i%2==0 else (("fixed",FIX),("stock",BASE))):
            assert run_cell(f"{i:02d}-{name}",source,INTERNAL)
    for name in ("stock","fixed"):
        rows=[row for i in range(12) for row in REPORT["runs"][f"{i:02d}-{name}"]["benchmark"]]
        assert all(REPORT["runs"][f"{i:02d}-{name}"]["binary"]==REPORT["runs"][name+"-sender"]["binary"] for i in range(12))
        (OUT/(name+".txt")).write_text("\n".join(rows)+"\n")
    assert REPORT["runs"]["stock-sender"]["binary"] != REPORT["runs"]["fixed-sender"]["binary"]
    target="@org_golang_x_perf//cmd/benchstat:benchstat"
    module=Path("MODULE.bazel")
    with module.open("a") as f:
        f.write("\n"+(WORK/"benchstat.module").read_text())
    REPORT["runs"]["reporter"]={}
    assert bazel("reporter","build",target)==0
    exe,=[Path(p).resolve() for p in subprocess.check_output([*BZ,"cquery",*FLAGS,"--output=files",target],text=True).splitlines() if p.startswith("bazel-out/") and Path(p).is_file()]
    stat=subprocess.check_output([exe,"stock.txt","fixed.txt"],cwd=OUT,text=True)
    (OUT/"benchstat.txt").write_text(stat)
    assert all(metric in stat for metric in ("sec/op","B/op","allocs/op","reserved-B/op","n=12"))
    REPORT["runs"]["reporter"]={"verified":True,"sha256":sha(exe.read_bytes())}
    REPORT["verified"]=all(x.get("verified") for x in REPORT["runs"].values())
    save()
    assert REPORT["verified"]


try:
    main()
except Exception as err:
    import traceback
    REPORT["error"]=f"{type(err).__name__}: {err}"[:500]
    REPORT["line"]=traceback.extract_tb(err.__traceback__)[-1].lineno
    save()
    raise
