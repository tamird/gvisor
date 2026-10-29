"""Report only the exact declared Codespell actions; keep test failures native."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess


SOURCE = "84fb13cc46f39b7eed90cb43139fb1b2928fdac8"
TREE = "4a1d4f82821f8d26f1708e78e40b0073daf3f0a2"
STATIC = "02939080d36c0fd2dc2507ff146a4c86360fa3968f30e1e42a6424d352629394"
IMAGES = {
    "scratch":"docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef",
    "ubuntu":"docker://mirror.gcr.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02",
}
WHEEL = json.loads(Path(__file__).with_name("wheel.json").read_text())
BASE = Path(os.environ["RUNNER_TEMP"])
RAW = BASE/"spelling-raw"
P = BASE/"spelling-evidence"
P.mkdir()
RAW.mkdir()
CASE = Path("pkg/.spelling-declared-new")
NEW = CASE/"notes with space.md"
DASH = Path("-relevant new.md")
CFG = "tools/.codespellrc"
RESULT = {"verdict":"INCOMPLETE","source":SOURCE,"tree":TREE,"phases":[]}


def need(ok, what):
  if not ok:
    raise RuntimeError(what)


def now():
  return datetime.now(timezone.utc).isoformat()


def git(*args):
  return subprocess.check_output(["git", *args])


def sha(b):
  return sha256(b).hexdigest()


def fid(b):
  return {"sha256":sha(b),"bytes":len(b)}


def records(path):
  data=path.read_text()
  decoder=json.JSONDecoder()
  pos=0
  while pos<len(data):
    if data[pos].isspace():
      pos+=1
      continue
    v,pos=decoder.raw_decode(data,pos)
    yield v


def inventory(sources, name):
  q=subprocess.run(["bazel","--bazelrc="+os.environ["SPELLING_RC"],"query","--output=label","labels(srcs, @spelling_sources//:inputs)"],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
  row={"phase":name,"exit":q.returncode,"stdout":fid(q.stdout),"stderr":fid(q.stderr)}
  RESULT.setdefault("queries",[]).append(row)
  need(q.returncode==0,"projected input query failed")
  paths={v.split("//:src/",1)[1] for v in q.stdout.decode().splitlines() if "//:src/" in v}
  row["count"]=len(paths)
  need(paths==sources|{CFG},"projected sources or config differ")


def phase(name, platform, sources, action_exit=0, test=False):
  d=RAW/name
  d.mkdir()
  be,ex,raw=d/"events.json",d/"execution.json",d/"output.log"
  labels=["//:codespell_test","//tools:lint_test"] if test else ["//:codespell"]
  plat="@spelling_qualification//:"+platform
  argv=["bazel","--bazelrc="+os.environ["SPELLING_RC"],"test" if test else "build","--host_platform="+plat,"--extra_execution_platforms="+plat,"--platforms=//tools/bazeldefs:linux_amd64","--noremote_accept_cached","--build_event_json_file="+str(be),"--execution_log_json_file="+str(ex),"--jobs=24"]
  if platform=="scratch":
    argv+=["--extra_toolchains=@spelling_qualification//:static_python"]
  if test:
    argv+=["--nocache_test_results"]
  row={"name":name,"started":now(),"platform":platform}
  RESULT["phases"].append(row)
  with raw.open("wb") as out:
    n=subprocess.run(argv+labels,stdout=out,stderr=subprocess.STDOUT)
  log=raw.read_bytes()
  message=log.decode(errors="replace")
  events=list(records(be)) if be.is_file() else []
  row.update(exit=n.returncode,finished=now(),log=fid(log),invocation=next((e["started"]["uuid"] for e in events if "started" in e),None),build_finish=[e["finished"].get("exitCode") for e in events if "finished" in e])
  errors=[line for line in message.splitlines() if line.startswith("ERROR:")]
  row["errors"]={"count":len(errors),"sha256":sha("\n".join(errors).encode()),"categories":[key for key,marker in {"analysis_syntax":"syntax error","visibility":"not visible","toolchain":"No matching toolchains","wheel_entry":"codespell_lib/__main__.py","import":"ModuleNotFoundError","attribute":"no field or method","repository":"no such package","file":"no such target","loading":"error loading package","declaration":"not declared"}.items() if marker in message]}
  rows=list(records(ex)) if ex.is_file() else []
  owners=[r for r in rows if r.get("mnemonic")=="CodespellCheck" and r.get("targetLabel")=="//:codespell"]
  need(len(owners)==1,"missing unique native CodespellCheck")
  a=owners[0]
  props={p["name"]:p["value"] for p in a["platform"]["properties"]}
  cmd=a["commandArgs"]
  row.update(action_exit=a.get("exitCode",0),runner=a.get("runner"),cached=a.get("cacheHit",False),placement={n:props.get(n) for n in ("container-image","Arch","OSFamily","workload-isolation-type")})
  need(row["runner"]=="remote" and not row["cached"] and props.get("Arch")=="amd64" and props.get("container-image")==IMAGES[platform],"wrong native worker")
  program,isolated,driver,main,manifest,marker=cmd
  need(isolated=="-IB" and driver=="tools/lint/spelling.py" and main.endswith("/codespell_lib/__main__.py") and manifest.endswith("/paths.json"),"wrong declared interpreter/driver/module")
  inputs={r["path"]:r["digest"] for r in a["inputs"] if "digest" in r}
  row["interpreter"]=inputs[program]
  need((inputs[program]["hash"]==STATIC)==(platform=="scratch"),"actual interpreter selection")
  wheelroot=main.removesuffix("codespell_lib/__main__.py")
  for path,expected in WHEEL.items():
    actual=inputs.get(wheelroot+path)
    need(actual and actual["hash"]==expected["hash"] and int(actual.get("sizeBytes",0))==expected["sizeBytes"],"wrong declared wheel bytes: "+path)
  row["wheel_files"]=len(WHEEL)
  row["wheel_main"]=inputs[main]
  row["builtin_dictionary"]=inputs[wheelroot+"codespell_lib/data/dictionary.txt"]
  root=str(Path(manifest).parent)+"/src/"
  files={k.removeprefix(root):v for k,v in inputs.items() if k.startswith(root)}
  need(set(files)==sources|{CFG},"physical Codespell input set")
  for k,v in files.items():
    b=Path(k).read_bytes()
    need(v["hash"]==sha(b) and int(v.get("sizeBytes",0))==len(b),"physical source bytes differ")
  value={"data":[CFG],"sources":sorted(sources)}
  need(inputs[manifest]["hash"]==sha(json.dumps(value,ensure_ascii=False,separators=(",",":")).encode()),"source names differ from actual manifest")
  row.update(count=len(sources),source_membership=sha(("\0".join(sorted(sources))+"\0").encode()),content=sha(json.dumps({k:v["hash"] for k,v in files.items()},sort_keys=True).encode()),manifest=inputs[manifest],config=files[CFG],driver=inputs[driver])
  row["diagnostic"]=[line for line in message.splitlines() if re.fullmatch(re.escape(str(NEW))+r":1: teh ==> the", line.strip())]
  row["no_write_guard"]="the source spelling check cannot enable write-changes" in message
  outs=a.get("actualOutputs",[])
  need(row["action_exit"]==action_exit and n.returncode==int(action_exit!=0),"actual exit differs")
  if action_exit:
    need(not outs and (bool(row["diagnostic"]) if action_exit==65 else row["no_write_guard"]),"incorrect source failure")
  else:
    need(len(outs)==1 and outs[0]["path"]==marker and outs[0]["digest"]["hash"]==sha(b""),"bad success marker")
  if test:
    status={e["id"]["testResult"]["label"]:e["testResult"]["status"] for e in events if "testResult" in e}
    need(status=={k:"PASSED" for k in labels},"native test results")
    row["test_results"]=status
    physical={}
    for r in rows:
      if r.get("mnemonic")!="TestRunner":
        continue
      k=r.get("targetLabel")
      if k in labels and r.get("runner")=="remote" and not r.get("cacheHit",False) and not r.get("exitCode",0) and any(v.endswith("/test-setup.sh") for v in r.get("commandArgs",[])):
        physical[k]={"test_log":[v.get("digest") for v in r.get("actualOutputs",[]) if v["path"].endswith("/test.log")]}
    need(set(physical)==set(labels) and all(v["test_log"] for v in physical.values()),"missing original native tests")
    row["physical_test_runners"]=physical
  row["verified"]=True


def main():
  need(git("rev-parse","HEAD","HEAD^{tree}").decode().splitlines()==[SOURCE,TREE],"wrong source or tree")
  need(not git("status","--porcelain","--untracked-files=no"),"dirty tracked sources")
  files={x.decode() for x in git("ls-files","-z","--","*.md","*.html","*.go","*.c","*.cc","*.h").split(b"\0") if x}
  need(len(files)==3283,"wrong tracked baseline")
  initial=Path(CFG).read_bytes()
  inventory(files,"clean")
  phase("scratch-clean","scratch",files)
  CASE.mkdir()
  (CASE/"BUILD.bazel").write_text("# New user package without lint hooks.\n")
  dirty=b"This is teh spelling probe. The name parth is accepted.\n"
  NEW.write_bytes(dirty)
  DASH.write_text("The name parth is accepted.\n")
  nested=CASE/"unrelated-repo"
  nested.mkdir()
  git("init","--quiet",str(nested))
  (nested/"ignore.go").write_bytes(dirty)
  (CASE/"ignored-link.md").symlink_to(nested/"ignore.go")
  files|={str(NEW),str(DASH)}
  inventory(files,"untracked filenames and nested repository")
  phase("scratch-new-file","scratch",files,65)
  need(NEW.read_bytes()==dirty,"headless typo check edited source")
  Path(CFG).write_bytes(initial+b"  teh,\n")
  phase("scratch-configured","scratch",files)
  phase("ordinary-default-runtime-tests","ubuntu",files,test=True)
  Path(CFG).write_bytes(initial+b"\nwrite-changes =\n")
  phase("scratch-refuses-automatic-edits","scratch",files,1)
  need(NEW.read_bytes()==dirty,"automatic edit escaped check")
  Path(CFG).write_bytes(initial)
  need(not git("diff","--name-only"),"unexpected tracked edits")
  RESULT["checked_input"]=fid(dirty)
  RESULT["verdict"]="PASS"


try:
  main()
except Exception as e:
  RESULT["error"]={"type":type(e).__name__,"reason":str(e)[:120] if isinstance(e,RuntimeError) else None}
  raise
finally:
  RESULT["verified_at"]=now()
  (P/"result.json").write_text(json.dumps(RESULT,indent=2,sort_keys=True)+"\n")
