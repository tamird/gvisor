"""Run both decoders against one private, native Nginx checkpoint."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from_root=Path(os.environ["GITHUB_WORKSPACE"])
tmp=Path(os.environ["RUNNER_TEMP"])
out=tmp/"profile-proof"
out.mkdir(exist_ok=True)
source=os.environ["SOURCE"]
baseline=os.environ["PROFILE_BASE"]
product=os.environ["PRODUCT"]
carrier=os.environ["GITHUB_SHA"]
container=["docker","exec","-e","GIT_CONFIG_COUNT=1","-e","GIT_CONFIG_KEY_0=safe.directory","-e","GIT_CONFIG_VALUE_0="+str(from_root),"overlay-guest"]
bazel=[*container,"bazel","--bazelrc="+str(tmp/"overlay.bazelrc")]
options=["--config=x86_64","-c","opt","--jobs=32","--strategy=GoStaticAnalysis=linux-sandbox","--strategy=GoStandardLibraryAnalysis=linux-sandbox","--strategy=GoStdlib=linux-sandbox","--strategy=GoStdlibList=linux-sandbox","--modify_execution_info=^Go(StandardLibrary|Static)Analysis$=-no-sandbox,^Go(Stdlib(List)?|StandardLibraryAnalysis)$=+no-remote-cache","--remote_download_outputs=toplevel"]
test_options=["--strategy=TestRunner=remote","--nocache_test_results","--test_timeout=750","--remote_download_regex=.*(outputs[.]zip|test[.]log|test[.]xml)$","--zip_undeclared_test_outputs","--test_output=errors"]
q="//test/benchmarks/checkpointfile:checkpointfile_test"
test="//test/rbe/graph_profile:nginx"
owners=["//pkg/state:state_test","//pkg/state/tests:tests_test","//pkg/state/wire:wire_test"]
expected={str(i)+"-"+a for i in range(1,9) for a in ("control","candidate")}
result={"upstream":os.environ["UPSTREAM_SHA"],"product":product,"benchmark_sources":{"control":baseline,"candidate":source},"carrier":carrier,"actions":[],"benchmarks":[],"suites":{},"builds":{},"failures":[]}
def now():return datetime.now(timezone.utc).isoformat()
def emit():
    result["absent_benchmarks"]=sorted(expected-set(result["benchmarks"]))
    (out/"verdict.json").write_text(json.dumps(result,indent=2)+"\n")
def git(*args):return subprocess.check_output(["git",*args],text=True).strip()
def checkout(sha):
    git("checkout","--quiet","--detach",sha)
    assert git("rev-parse","HEAD")==sha
def action(name,kind,args,targets):
    log=tmp/(name+".log")
    bep=tmp/(name+".bep")
    flags=[*options,*args,"--build_event_json_file="+str(bep),*targets]
    step={"phase":name,"source":git("rev-parse","HEAD"),"targets":targets,"start":now()}
    result["actions"].append(step)
    with log.open("wb") as f:p=subprocess.run(bazel+[kind]+flags,stdout=f,stderr=subprocess.STDOUT)
    step.update(exit=p.returncode,finish=now())
    lines=log.read_text(errors="replace").splitlines()
    step["invocations"]=sorted(set(re.findall(r'https://app.buildbuddy.io/invocation/[\da-f-]+',"\n".join(lines))))
    if bep.is_file():
        step["summaries"]={e["id"]["testSummary"]["label"]:e["testSummary"]["overallStatus"] for e in map(json.loads,bep.open()) if "testSummary" in e}
    if p.returncode:
        safe=[r[:420] for r in lines if re.search(r'ERROR:|FAIL|fatal|RUN:|skip|killed|timeout',r,re.I) and not re.search(r'api[-_ ]?key|remote_header|bes_header|token|authorization:|secret',r,re.I)]
        (out/(name+"-diagnostics.txt")).write_text("\n".join(safe[-36:])+"\n")
        result["failures"].append(name+" exited "+str(p.returncode))
    emit()
    return step
def examine_calls(arm, binary):
    # Prefer the final linked binary. If opt strips its symbols, the already
    # built library archive still has the original Go call relocations.
    check = {"source":git("rev-parse","HEAD"),"attempts":[]}
    result.setdefault("assembly",{})[arm]=check
    pattern = r"gvisor.dev/gvisor/pkg/state\.\(\*graphDecodeState\)\.decodeValue$"
    def examine(path, kind):
        proc=subprocess.run(["go","tool","objdump","-s",pattern,str(path)],capture_output=True,text=True)
        lines=proc.stdout.splitlines()
        calls=[s.strip() for s in lines if re.search(r'\bCALL\b',s) and "(*decBuf).uvarint" in s]
        found=any("TEXT" in s and "decodeValue" in s for s in lines)
        check["attempts"].append({"kind":kind,"exit":proc.returncode,"function_found":found,"uvarint_calls":calls})
        return found and proc.returncode==0
    if examine(binary,"linked checkpointfile_test"):
        return
    files=subprocess.check_output([*bazel,"cquery",*options,"--output=files","//pkg/state:state"],stderr=subprocess.DEVNULL,text=True).splitlines()
    for src in files:
        if src.endswith(".a"):
            dst=tmp/("state-"+arm+".a")
            cp=subprocess.run(container+["cp","-L",src,str(dst)])
            if cp.returncode==0 and examine(dst,"state Go archive"):
                return

def qual_build(arm,sha):
    checkout(sha)
    step=action(arm+"-compile","build",[],[q,"//pkg/state:state"])
    if step["exit"]:return
    # Use the declared target output rather than the symlinks of another config.
    filelist=subprocess.check_output([*bazel,"cquery",*options,"--output=files",q],stderr=subprocess.DEVNULL,text=True).splitlines()
    paths=[path for path in filelist if subprocess.run(container+["test","-x",path]).returncode==0]
    assert len(paths)==1,paths
    path=tmp/("graph-"+arm+".bin")
    subprocess.run(container+["cp","-L",paths[0],str(path)],check=True)
    result["builds"][arm]={"sha256":hashlib.file_digest(path.open("rb"),"sha256").hexdigest(),"size":path.stat().st_size}
    examine_calls(arm,path)
    emit()

emit()
try:
    git("diff","--exit-code",result["upstream"],baseline,"--","pkg/state","pkg/sentry","runsc")
    git("diff","--exit-code",baseline,source,"--","test/benchmarks/checkpointfile")
    git("diff","--exit-code",product,source,"--","pkg/state","pkg/sentry","runsc")
    git("diff","--exit-code",baseline,carrier,"--","pkg/state","pkg/sentry","runsc")
    for arm,sha in result["benchmark_sources"].items():qual_build(arm,sha)
    checkout(source)
    arm="candidate"
    labels=owners
    options2=test_options+["--test_arg=-test.v"]
    step=action(arm+"-state-tests","test",options2,labels)
    if step.get("summaries")!=dict.fromkeys(labels,"PASSED"):result["failures"].append(arm+" state suite incomplete")
    for label in labels:
        x="bazel-testlogs/"+label.removeprefix("//").replace(":","/")+"/test.log"
        p=subprocess.run(container+["cat",x],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        lines=p.stdout.decode(errors="replace").splitlines()
        cases=[x.strip() for x in lines if re.match(r"^\s*--- (PASS|SKIP|FAIL): Test",x)]
        (out/(arm+"-"+label.split(":")[-1]+"-names.txt")).write_text("\n".join(cases)+"\n")
        if label==owners[0]:
            required=["TestEncDecBufRoundTrip","TestDecBufErrors","TestPooledEncoderScratchReleasesPointers"]
        elif label==owners[1]:
            required=["TestAbsorbedInlineStructSplices","TestAbsorbedSupersededReferencesPruned","TestOverlappingArrayPointersAndSlicesGraph","TestInteriorPointersIntoSlices","TestMapLoadHookReceivers","TestSharedSliceWindows"]
        else:
            required=["TestArrayPayloadWireRoundTrip"]
        if not all(any(re.match(r"--- PASS: "+k+r" +\(",x) for x in cases) for k in required):result["failures"].append(label+" missing a required case on "+arm)
        result["suites"][arm+" "+label]={"pass":sum(x.startswith("--- PASS:") for x in cases),"skip":[x for x in cases if x.startswith("--- SKIP:")],"fail":[x for x in cases if x.startswith("--- FAIL:")]}
    checkout(carrier)
    shutil.move(tmp/"nginx.tar",from_root/"test/rbe/graph_profile/nginx.tar")
    assert set(result["builds"])=={"control","candidate"},result["builds"]
    assert result["builds"]["control"]["sha256"]!=result["builds"]["candidate"]["sha256"]
    for arm in result["builds"]:shutil.copy(tmp/("graph-"+arm+".bin"),from_root/"test/rbe/graph_profile"/(arm+".bin"))
    paired=action("paired-checkpoint","test",test_options,[test])
    file=tmp/"paired.zip"
    with file.open("wb") as f:subprocess.run(container+["cat","bazel-testlogs/test/rbe/graph_profile/nginx/test.outputs/outputs.zip"],stdout=f,check=True)
    with ZipFile(file) as z:
        for p in z.namelist():
            n=Path(p).name
            if re.fullmatch(r'(input|input-after|binaries|order|control-roundtrip|candidate-roundtrip)\.txt|[1-8]-(control|candidate)\.bench',n):(out/n).write_bytes(z.read(p))
    for arm in result["builds"]:
        assert result["builds"][arm]["sha256"]+" " in (out/"binaries.txt").read_text()
        assert "PASS: TestCheckpointFileRoundTrip" in (out/(arm+"-roundtrip.txt")).read_text()
    assert (out/"input-after.txt").read_text().count(": OK")==4
    order=[line.split() for line in (out/"order.txt").read_text().splitlines()]
    assert order==[[str(i),a] for i in range(1,9) for a in (("control","candidate") if i%2 else ("candidate","control"))]
    for s in sorted(expected):
        if re.search(r'BenchmarkCheckpointFile/DecodeStatefile(?:-\d+)?\s+[1-9]\d*\s+\d+ ns/op.*\d+ B/op\s+\d+ allocs/op',(out/(s+".bench")).read_text()):result["benchmarks"].append(s)
    for arm in ("control","candidate"):
        data=[(out/(str(i)+"-"+arm+".bench")).read_text().strip() for i in range(1,9)]
        (out/(arm+".bench")).write_text("\n".join(data)+"\n")
    assert paired.get("summaries")=={test:"PASSED"}
except Exception as e:result["failures"].append(type(e).__name__+": "+str(e)[:400])
finally:
    result["verified_at"]=now();emit()
assert not result["failures"] and not result["absent_benchmarks"],result["failures"]
